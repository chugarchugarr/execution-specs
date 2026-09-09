use lean_compiler::{compile, parse, parse_with_replacements};
use lean_vm::cpu::{fs_seed, layout::bytecode_table, prove, verify, Program};
use lean_vm::transcript::{FiatShamirState, ProverState, Receiver, Transmitter, VerifierState};
use pcs::whir::{
    build_eq_table_ext, commit, config_for_rate, inner_product_base_ext,
    recursive_prover_with_basis, recursive_verifier_with_basis_succinct,
};
use primitives::{field::{F64, F192}, hash::Hasher, multilinear::{eq_eval, mle_eval_par}};
use rec_aggregation::aggregation::{placeholder_map, ps_gen_verify};

const LABEL: &[u8] = b"eip8288/leanvm-program-vk/v1";
const PROGRAM_VK_LOG_N: usize = 22;
const LOG_INV_RATE: usize = 1;

fn pack_hash_state(hash: &[u8; 32]) -> [F192; 2] {
    let w = |o: usize| u64::from_le_bytes(hash[o..o + 8].try_into().unwrap());
    [F192::new(w(0), w(8), 0), F192::new(w(16), w(24), 0)]
}
fn pack_state(state: [F64; 4]) -> [F192; 2] {
    [F192::new(state[0].0, state[1].0, 0), F192::new(state[2].0, state[3].0, 0)]
}
fn seed_bytes(seed: [F192; 2]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[0..8].copy_from_slice(&seed[0].c0.to_le_bytes());
    out[8..16].copy_from_slice(&seed[0].c1.to_le_bytes());
    out[16..24].copy_from_slice(&seed[1].c0.to_le_bytes());
    out[24..32].copy_from_slice(&seed[1].c1.to_le_bytes());
    out
}
fn f192(x: F192) -> String { format!("f192({},{},{})", x.c0, x.c1, x.c2) }
fn program(source: &str) -> Program { compile(&parse(source).expect("program parses")) }

fn vk_hash(seed: [F192; 2], root: &[u8; 32], inner_vars: usize) -> [u8; 32] {
    let mut h = Hasher::new();
    h.update(b"eip8288/leanvm-program-vk/v2");
    h.update(&seed_bytes(seed));
    h.update(root);
    h.update(&(inner_vars as u64).to_le_bytes());
    h.update(&(PROGRAM_VK_LOG_N as u64).to_le_bytes());
    h.update(&(LOG_INV_RATE as u64).to_le_bytes());
    h.finalize()
}

fn opening_for(table: &[F64], seed: [F192; 2], point: &[F192], value: F192) -> ([u8;32], fiat_shamir::transcript::RawProof) {
    assert_eq!(table.len(), 1usize << PROGRAM_VK_LOG_N);
    assert_eq!(point.len(), PROGRAM_VK_LOG_N);
    assert_eq!(mle_eval_par(table, point), value, "ProgramVK polynomial must be the exact deferred bytecode claim");
    let cfg = config_for_rate(PROGRAM_VK_LOG_N, LOG_INV_RATE).unwrap();
    let basis = build_eq_table_ext(point);
    assert_eq!(inner_product_base_ext(table, &basis), value);
    let (commitment, pd) = commit(table, PROGRAM_VK_LOG_N, cfg.initial_k, cfg.log_inv_rates[0]);
    let root = commitment.root;
    let mut ps = ProverState::from_label(LABEL);
    ps.add_root(&root);
    ps.add_scalar(seed[0]); ps.add_scalar(seed[1]);
    for &x in point { ps.add_scalar(x); }
    ps.add_scalar(value);
    recursive_prover_with_basis(
        &cfg, PROGRAM_VK_LOG_N, table, zk_alloc::ArenaVec::from_slice(&basis), value,
        &pd.codeword, &pd.merkle_tree, &mut ps,
    );
    let proof = ps.into_proof();
    let mut vs = VerifierState::from_label(LABEL, &proof);
    assert_eq!(vs.next_root().unwrap(), root);
    assert_eq!(vs.next_scalar().unwrap(), seed[0]); assert_eq!(vs.next_scalar().unwrap(), seed[1]);
    for &x in point { assert_eq!(vs.next_scalar().unwrap(), x); }
    assert_eq!(vs.next_scalar().unwrap(), value);
    assert!(recursive_verifier_with_basis_succinct(
        &cfg, PROGRAM_VK_LOG_N, 1usize << cfg.initial_k, value, &root,
        |fold_point| eq_eval(point, fold_point), &mut vs,
    ).is_ok());
    vs.finish().unwrap();
    (root, vs.into_raw_proof())
}

fn main() {
    // Deliberately NOT the aggregation guest: max-disagreement test for whether
    // current verify_sub is program-generic once its exact layout replacements/hints are supplied.
    let mut inner = program("def main():\n    return\n");
    inner.min_log_committed = 22;
    let public = [F192::ZERO, F192::ZERO];
    let (inner_proof, _) = prove(&inner, public, LOG_INV_RATE);
    let summary = verify(&inner, &public, &inner_proof).expect("native arbitrary-program proof verifies");
    let (sub_hints, bc_point, bc_value) = ps_gen_verify(&inner, public, summary).expect("derive exact verify_sub witness");
    let kbc = inner.prog.len().trailing_zeros() as usize;
    assert!(bc_point.len() <= PROGRAM_VK_LOG_N);

    let mut table = bytecode_table(&inner.prog);
    assert!(table.len() <= 1usize << PROGRAM_VK_LOG_N);
    table.resize(1usize << PROGRAM_VK_LOG_N, F64::ZERO);

    let mut point_append = bc_point.clone();
    point_append.resize(PROGRAM_VK_LOG_N, F192::ZERO);
    let mut point_prepend = vec![F192::ZERO; PROGRAM_VK_LOG_N - bc_point.len()];
    point_prepend.extend_from_slice(&bc_point);
    let append_ok = mle_eval_par(&table, &point_append) == bc_value;
    let prepend_ok = mle_eval_par(&table, &point_prepend) == bc_value;
    assert!(append_ok ^ prepend_ok || (append_ok && point_append == point_prepend), "deferred point must have a deterministic ProgramVK embedding");
    let ext_point = if append_ok { point_append } else { point_prepend };
    let prepend = !append_ok;

    let seed = fs_seed(&inner);
    let (root, raw_opening) = opening_for(&table, seed, &ext_point, bc_value);
    let vkh = vk_hash(seed, &root, bc_point.len());
    let root_cells = pack_hash_state(&root);
    let label_state = pack_state(FiatShamirState::from_label(LABEL).state());
    const PREFIX: usize = 2 + 2 + PROGRAM_VK_LOG_N + 1;
    assert!(raw_opening.stream.len() >= PREFIX);
    let opening_suffix = raw_opening.stream[PREFIX..].to_vec();
    let mut opening_rows = Vec::<F192>::new();
    let mut opening_paths = Vec::<F192>::new();
    for op in &raw_opening.merkle {
        opening_rows.extend(op.leaf_data.iter().map(|x| F192::new(x.0, 0, 0)));
        for h in &op.path { opening_paths.extend_from_slice(&pack_hash_state(h)); }
    }

    let base = include_str!("../guests/lean_ethereum.py");
    let mut src = base.replacen("def main():", "def aggregation_main():", 1);
    let constants = format!(r#"
PS_PI_0 = {pi0}
PS_PI_1 = {pi1}
PS_SEED_0 = {seed0}
PS_SEED_1 = {seed1}
PS_VK_ROOT_0 = {root0}
PS_VK_ROOT_1 = {root1}
PS_LABEL_0 = {label0}
PS_LABEL_1 = {label1}
PS_PROGRAM_VK_LOG_N = {pvkn}
PS_POINT_PREPEND = {prepend}
PS_OPEN_STREAM_CAP = {stream_cap}
"#,
        pi0=f192(public[0]), pi1=f192(public[1]), seed0=f192(seed[0]), seed1=f192(seed[1]),
        root0=f192(root_cells[0]), root1=f192(root_cells[1]), label0=f192(label_state[0]), label1=f192(label_state[1]),
        pvkn=PROGRAM_VK_LOG_N, prepend=usize::from(prepend), stream_cap=opening_suffix.len());
    let first = [src.find("\n@"), src.find("\ndef ")].into_iter().flatten().min().unwrap() + 1;
    src.insert_str(first, &constants);
    src.push_str(r#"

def main():
    g_logs_pow2, g_squares = exponent_tables()
    defer = HeapBuf(DEFER_SIZE)
    verify_sub(PS_PI_0, PS_PI_1, PS_SEED_0, PS_SEED_1, g_logs_pow2, g_squares, defer)

    fs = StackBuf(2)
    fs[0] = PS_LABEL_0
    fs[1] = PS_LABEL_1
    fs = obs(fs, PS_VK_ROOT_0)
    fs = obs(fs, PS_VK_ROOT_1)
    fs = obs(fs, PS_SEED_0)
    fs = obs(fs, PS_SEED_1)
    if PS_POINT_PREPEND == 1:
        for k in unroll(0, PS_PROGRAM_VK_LOG_N - BYTECODE_VARS):
            fs = obs(fs, 0)
        for k in unroll(0, BYTECODE_VARS):
            fs = obs(fs, defer[GEN ** k])
    else:
        for k in unroll(0, BYTECODE_VARS):
            fs = obs(fs, defer[GEN ** k])
        for k in unroll(BYTECODE_VARS, PS_PROGRAM_VK_LOG_N):
            fs = obs(fs, 0)
    bc_value = defer[GEN ** FRESH_BC_VALUE]
    fs = obs(fs, bc_value)

    stream = HeapBuf(PS_OPEN_STREAM_CAP)
    hint_witness(stream[0:PS_OPEN_STREAM_CAP], "ps_vk_stream")
    target, fold_point, inner_total, yr_log_n_g, yr_pad_g, fold_cap_g, tail, yr_at_tail = open_stacked(0, fs[0], fs[1], bc_value, PS_VK_ROOT_0, PS_VK_ROOT_1, stream)
    eq = GEN ** 0
    if PS_POINT_PREPEND == 1:
        for k in unroll(0, PS_PROGRAM_VK_LOG_N - BYTECODE_VARS):
            eq *= (1 + fold_point[GEN ** k])
        for k in unroll(0, BYTECODE_VARS):
            eq *= (1 + defer[GEN ** k] + fold_point[GEN ** (PS_PROGRAM_VK_LOG_N - BYTECODE_VARS + k)])
    else:
        for k in unroll(0, BYTECODE_VARS):
            eq *= (1 + defer[GEN ** k] + fold_point[GEN ** k])
        for k in unroll(BYTECODE_VARS, PS_PROGRAM_VK_LOG_N):
            eq *= (1 + fold_point[GEN ** k])
    assert (inner_total + eq) * yr_at_tail == target
    return
"#);

    let repl = placeholder_map(kbc);
    let ast = parse_with_replacements(&src, &repl).expect("generic direct-verifier guest parses");
    let mut guest = compile(&ast);

    // hint_witness streams are consumed FIFO by NAME. verify_sub and open_stacked
    // intentionally share the production Merkle hint names, so preserve the
    // inner proof's first entry and append ProgramVK's opening as the second.
    let mut had_rows = false;
    let mut had_paths = false;
    for (name, vals) in sub_hints {
        match name {
            "merkle_leaf_rows" => {
                guest.set_witness(name, vec![vals, opening_rows.clone()]);
                had_rows = true;
            }
            "merkle_paths" => {
                guest.set_witness(name, vec![vals, opening_paths.clone()]);
                had_paths = true;
            }
            _ => guest.set_witness(name, vec![vals]),
        }
    }
    assert!(had_rows && had_paths, "verify_sub must provide its authenticated Merkle hints");
    guest.set_witness("ps_vk_stream", vec![opening_suffix]);

    let outer_public = [F192::ZERO, F192::ZERO];
    let (proof, _) = prove(&guest, outer_public, LOG_INV_RATE);
    verify(&guest, &outer_public, &proof).expect("arbitrary verify_sub + ProgramVK closure verifies in leanVM");

    println!("ARBITRARY_INNER_KBC={kbc}");
    println!("ARBITRARY_DEFERRED_POINT_VARS={}", bc_point.len());
    println!("PROGRAM_VK_EMBED_PREPEND={prepend}");
    println!("PROGRAM_VK_HASH={}", vkh.iter().map(|b| format!("{b:02x}")).collect::<String>());
    println!("ARBITRARY_VERIFY_SUB_PASS=true");
    println!("EXACT_DEFERRED_BYTECODE_CLOSURE_PASS=true");
    println!("ARBITRARY_DIRECT_LEANSTARK_KERNEL_PASS=true");
    println!("DIRECT_KERNEL_GUEST_INSTRUCTIONS={}", guest.prog.len());
}
