# Models and runtime

Downloaded 2026-10-29. All hashes match the values published by the source.

| File | Source (pinned revision) | Size | sha256 |
|---|---|---|---|
| `qwen2.5-0.5b-instruct-q4_k_m.gguf` | huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF @ `9217f5db79a29953eb74d5343926648285ec7e67` | 491,400,032 B | `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db` |
| `SmolLM2-360M-Instruct-Q4_K_M.gguf` | huggingface.co/unsloth/SmolLM2-360M-Instruct-GGUF @ `391ed11137586e383b1be0fab9acf01d282c2e11` | 270,590,560 B | `16c7f1667fea34bacad196a57b548effcb37614db4ab5677a20c8c7b823b9e63` |
| `runtime/downloads/llama-b11606-bin-win-cpu-x64.zip` | github.com/ggml-org/llama.cpp release `b11606` (2026-10-13; commit `1b70350c9`) | 19,514,151 B | `8c0f4f2a1b808eebe00e738edf027cceea1aa9e6c883618bd37151666093f1b4` |

The llama.cpp zip is extracted to `runtime/llama-b11606/`. It is a Windows x86-64 CPU build used only as a development proxy; build llama.cpp natively on the ARM target for benchmarks that are reported.

Benchmark results: `bench/results/`, decision record: `docs/g0_decision.md`.
