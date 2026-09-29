# chipgraph — Implementation Plan

| Mục | Giá trị |
|---|---|
| Phiên bản | v1.0, 2026-09-27 |
| Dựa trên | [`../DESIGN.md`](../DESIGN.md) v0.2 · [`DECISIONS.md`](DECISIONS.md) D1–D34 |
| Người đọc | Các agent (và người) implement `chipgraph` |
| Lead | Trong Nghia — duyệt mọi PR, quyết mọi thay đổi contract |

Tài liệu này chia việc thành **task nhỏ có ID, phụ thuộc, output và tiêu chí nghiệm thu**,
để nhiều agent làm song song mà không dẫm lên nhau. Luật làm việc nằm ở
[`../AGENTS.md`](../AGENTS.md).

---

## 0. Cách dùng kế hoạch này

1. Đọc `AGENTS.md`, `DESIGN.md`, rồi mục của task mình nhận.
2. Chỉ nhận task có **mọi `depends` đã merge**.
3. Chỉ sửa file trong **`writes`** của task. Cần sửa ngoài phạm vi thì mở issue, không tự sửa.
4. Mỗi task là **một PR**, branch `feat/<task-id>-<slug>` (ví dụ `feat/m0-06-graph`).
5. Xong khi **mọi tiêu chí `accept` pass trong CI** và lead duyệt.
6. **Không đổi contract** (`src/chipgraph/core/contracts/`, `plugin_api/`) nếu không có
   task hoặc ADR cho việc đó. Contract đổi thì mọi agent khác vỡ.

### 0.1 Giả định nền

| Chủ đề | Giả định (nếu lead đổi thì cập nhật ở đây) |
|---|---|
| Quyết định | **Đã chốt toàn bộ D1–D34** (2026-09-27): build graph là lõi, Design Model, engine bằng code, mốc "Hiểu" trước sinh code, Python, Apache-2.0… |
| Tên | `chipgraph` (D22, đã chốt) |
| Repo | Public, Apache-2.0 (D23) |
| Ngôn ngữ | Python 3.14, hỗ trợ từ 3.13; `uv` (D21) |
| Model | **Mặc định dùng Claude của chính người dùng qua Claude Code** (runtime `claude-code`, D35). Runtime API (`claude-agent-sdk`, `generic`) cho CI, evals, GLM, model tự host (D24, D27) |
| Dự án pilot | QSoC (`vlsi_deep_training`, public, Apache-2.0), chạy **ngoài** repo này. Fixture lấy từ QSoC (contract, INTMAP, PWM, GPIO3) được phép copy vào repo này nhưng **phải giữ license và ghi nguồn**; IP vendor bên trong (ví dụ `apb_adv_timer` của PULP, license Solderpad) giữ nguyên header license. Không copy tài liệu của thầy (HAS) |
| Chi phí | Mọi lần gọi model đều qua budget; CI dùng model giả (fake) trừ job evals |
| Phạm vi đợt đầu | **M0 + M1 là bản dùng được đầu tiên.** Chỉ bắt đầu M2 khi M1 được team dùng hằng ngày và lead duyệt demo M1 |
| Còn mở | Trần chi phí cho CI và evals; người giữ token hoặc key cho CI và evals. Trước khi có con số, job evals thật chỉ chạy thủ công |

### 0.2 Luồng song song (lane)

| Lane | Phạm vi | Có thể chạy song song với |
|---|---|---|
| **A. Core** | contracts, config, engine, state | — (các lane khác phụ thuộc A ở M0) |
| **B. Adapters** | tool, parser, runner, vcs, review, llm | A sau khi có contracts (M0-02) |
| **C. Model** | Design Model, format adapter, extractor, checks chéo | A, B sau M0-02 |
| **D. Agent** | runtime, vai, skill, decide(), sandbox | C |
| **E. Surface** | CLI, MCP, plugin, site, dashboard | sau khi có API engine |
| **F. Infra** | repo, CI, Docker, release, evals harness | mọi lane |

---

## 1. Contract cốt lõi (khóa ở M0-02)

Mọi lane dựa vào các kiểu này. Tên trường là tiếng Anh; tất cả là pydantic v2 model, có
`schema_version`, và xuất JSON Schema vào `schemas/`.

| Kiểu | Trường chính |
|---|---|
| `ArtifactRef` | `repo` (id thành viên workspace; mặc định `.`), `kind` (spec, rtl, tb, report, plan, model, doc, diagram…), `path` hoặc `model_key`, `label` (public/internal/nda). Có `repo` ngay từ M0 để khỏi đổi contract khi làm workspace |
| `Artifact` | `ref`, `content_hash` (sha256), `produced_by` (rule id + run id), `inputs_hash` |
| `RuleSpec` | `id` (`<pack>/<name>`), `kind` (gen/agent/human/import), `foreach`, `inputs`, `outputs` (mẫu đường dẫn), `checks`, `verified_by`, `budget`, `role`, `skills`, `gate` |
| `RuleInstance` | `rule_id`, `params` (block, module…), `inputs` (ArtifactRef đã giải), `outputs` (đường dẫn cụ thể) |
| `CheckSpec` / `CheckResult` | spec: `id`, `capability`, `adapter`, `args`; result: `status` (pass/fail/error/skipped), `issues[{file,line,rule,severity,msg}]`, `log_tail`, `duration_s`, `idempotency_key` |
| `GateSpec` / `Approval` | gate: `id`, `artifacts`, `approvers`, `mode` (pr/file); approval: `gate_id`, `by`, `at`, `artifact_hashes`, `decision` (approve/reject/baseline/waive), `note`. Mỗi approval là một file trong `.chipgraph/decisions/` |
| `Event` | `run_id`, `seq`, `ts`, `type` (run_start, rule_start, tool_call, check_result, agent_turn, gate_wait, gate_decision, rule_done, rule_fail, run_stop…), `payload`, `failure_label` (context/constraint/verification/planning/infra) |
| `RunManifest` | versions (chipgraph, packs, adapters, model, tool EDA), profile hash, target, runner |
| `Profile` | xem DESIGN mục 6.1 (`state`, `decisions`), 7.3 (`naming`, `layout`), 8.3–8.4 (`extends`, tầng), 8.6 (`paths`, `templates`, `style`, `plugins`, tầng người dùng), 8.7 (`workspace`), 12.6 (`env`, `vcs`, `review`, `runner`, `runtime`, `offline`); model đầy đủ trong `core/config` |
| `AgentResult` | `files_written`, `status`, `assumptions[]`, `open_questions[]`, `tokens`, `cost` |
| `Decision` | `question_id`, `value`, `confidence`, `backend` (rule/small/large) |

**Protocol cho plugin** (`core/plugin_api/`): `ToolAdapter`, `LogParser`, `Runner`,
`FormatAdapter`, `VcsAdapter`, `ReviewAdapter`, `LlmProvider`, `AgentRuntime`, `Check`,
`Generator`, `Extractor`. Adapter đăng ký qua Python entry points
(`chipgraph.adapters.<loại>`); pack được nạp từ thư mục có `pack.yml`. Pack đi kèm tool nằm ở
`src/chipgraph/packs/<tên_snake>/`, luật tổ chức và preset ở `src/chipgraph/orgs/`,
`src/chipgraph/presets/` (D36). Task thêm code pack được thêm dòng entry point của mình vào
`pyproject.toml`.

---

## 2. Spike (làm trước, mỗi cái tối đa 2 ngày)

Spike là thử nhanh để giảm rủi ro. Kết quả là một ghi chú ngắn trong
`docs/spikes/<id>.md` (làm được gì, không được gì, khuyến nghị), không phải code production.

| ID | Câu hỏi | Ảnh hưởng tới |
|---|---|---|
| S1 | Claude Agent SDK (0.2.x) có chạy với endpoint tương thích Anthropic của GLM không? Có tool calling, MCP, giới hạn tool không? | D24, M1-10 |
| S2 | pyslang 11 trích được hierarchy, port, param, instance, clock/reset trên RTL của QSoC không (gồm wrapper có AUTO của emacs và IP vendor)? | M1-04 |
| S3 | Edalize 0.6 + cocotb 2.1 + Verilator 5.052 chạy một testbench trên macOS và Linux không? | M2-05 |
| S4 | NVIDIA OpenShell 0.1 chạy được trên macOS của team không, hay dùng container? | D11, M1-11 |
| S5 | MCP Python SDK 2.x: server stdio, và plugin Claude Code gọi được tool của nó | M0-14 |
| S7 | **Runtime `claude-code`:** plugin lặp `next_task` → subagent của vai → `submit`; subagent giới hạn được tool và model; hook `PreToolUse` chặn ghi ngoài `outputs`; nhiều subagent chạy song song; đọc được token qua OpenTelemetry của Claude Code | M1-11 |
| S6 | Model tự host (GLM open-weight, qua vLLM) cho tool calling ổn định tới mức nào? | D27, M5-06 |

S1–S5 và S7 làm song song ngay đầu M0. S6 làm trước M5.

---

## 3. M0 — Nền

**Mục tiêu:** engine chạy được build graph không có AI; resume đúng; chạy check của một dự án
thật qua adapter `cmd`.

| ID | Task | Lane | depends | writes | accept |
|---|---|---|---|---|---|
| M0-01 | Khởi tạo repo: `pyproject.toml` (uv, Python ≥3.13), `Makefile` (các lệnh ngắn: `schemas`, `test`, `lint`), ruff, mypy strict cho `core/`, pytest, pre-commit, `LICENSE` (Apache-2.0), `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`, `renovate.json`, `.github/workflows/ci.yml` (lint, type, test trên 3.13 và 3.14, macOS và Linux), secret scanning | F | — | gốc repo, `.github/` | CI xanh trên repo rỗng; `uv run chipgraph --version` in version |
| M0-02 | Contract cốt lõi (mục 1) + xuất JSON Schema + test round-trip | A | 01 | `src/chipgraph/core/contracts/`, `schemas/` | Mọi kiểu có test serialize/deserialize; `make schemas` tái tạo `schemas/` không diff |
| M0-03 | Plugin API: Protocol + registry qua entry points + nạp pack từ `pack.yml`; **import-linter** cấm `core` import `adapters`/`packs` | A | 02 | `core/plugin_api/`, `.importlinter` | Adapter giả đăng ký và được tìm thấy; CI fail nếu core import adapter |
| M0-04 | Config nhiều tầng: mặc định → org → preset → dự án → đường dẫn (`paths:`) → block, cộng tầng người dùng chỉ cho trải nghiệm cá nhân (DESIGN 8.6); `extends` từ git có version; **profile là `.chipgraph.yml` ở gốc repo** (DESIGN 8.4), `--profile` chỉ là lối thoát nâng cao (và dùng trong test); ghi commit của profile và của mọi `extends` vào run manifest; không có profile thì chỉ cho lệnh chỉ đọc; validate bằng schema; `config show --explain`, `config check` | A | 02 | `core/config/` | Test ghi đè từng tầng; người dùng không đổi được quy ước artifact (test); xung đột bị `config check` báo; lỗi schema chỉ rõ file và dòng; `extends` từ git được khóa version (test); không có profile thì `build` bị từ chối, lệnh chỉ đọc vẫn chạy và không ghi file (test) |
| M0-05 | Artifact store: hash nội dung, gán nhãn dữ liệu theo glob, VCS adapter `git` (commit hiện tại, file đã đổi, tạo và xóa worktree) | A+B | 02, 03 | `core/state/artifacts.py`, `adapters/vcs/git.py` | Hash ổn định; nhãn `nda` được áp đúng; worktree tạo và dọn sạch |
| M0-06 | Build graph: nạp `RuleSpec`, mở rộng `foreach`, giải mẫu đường dẫn output, dựng DAG, phát hiện chu trình, **phát hiện tập ghi giao nhau** (F2), tính **stale** từ `inputs_hash` đã ghi | A | 02, 05 | `core/engine/graph.py` | Test: chu trình bị báo lỗi; hai rule ghi cùng file bị báo lỗi; đổi một input thì đúng tập artifact phía sau thành stale |
| M0-07 | Journal và state: JSONL chỉ ghi thêm, dựng lại state từ journal, thư mục run, khóa theo block (file lock), khóa idempotency cho tool call; **state backend** `local` (`.chipgraph/state/`, tự thêm vào gitignore cục bộ qua `.git/info/exclude`) và `branch:<tên>` (branch mồ côi); `.chipgraph/decisions/` tách khỏi state (DESIGN 6.1) | A | 02 | `core/state/` | Ghi khi bị kill giữa chừng không làm hỏng journal; dựng lại state khớp; **chạy build không tạo file nào được git theo dõi** ngoài artifact và `.chipgraph/decisions/` (test) |
| M0-08 | Scheduler: chạy node sẵn sàng bằng asyncio, giới hạn song song, loại rule `gen`/`check`/`human`/`import` (loại `agent` là stub), dừng ở gate, `resume`, `rewind <step>` | A | 06, 07 | `core/engine/scheduler.py` | **Test resume:** kill ở từng loại bước rồi resume, kết quả giống chạy liền; `rewind` quay đúng |
| M0-09 | Framework check + adapter `cmd` + runner `local` + log parser `verilator`, `verible`, `generic-regex` | B | 03 | `adapters/tool/cmd.py`, `adapters/runner/local.py`, `adapters/parser/` | Chạy lệnh, thu `CheckResult` đúng schema; parser có test với log mẫu của Verilator 5.020 và 5.052 |
| M0-10 | Check dựng sẵn: `layout`, `filelist`, `generated` (DESIGN 7.3) | C | 04, 09 | `src/chipgraph/checks/` | Mỗi check có test pass và fail trên `examples/tinysoc` |
| M0-11 | Gate và duyệt: `ReviewAdapter` `file` (mỗi quyết định một file trong `.chipgraph/decisions/`, gắn hash; gate `pr` dùng review của PR), hết hiệu lực khi hash đổi | A+B | 06 | `core/engine/gate.py`, `adapters/review/file.py` | Duyệt rồi đổi artifact thì gate về "chờ" |
| M0-12 | CLI (typer): `init`, `config show`, `status`, `build <target>`, `resume`, `rewind`, `check [--block]`, `approve`, `doctor`; cờ `--profile <path>` (lối thoát nâng cao) | E | 04, 08, 11 | `src/chipgraph/cli/` | `chipgraph --help` đủ lệnh; test CLI bằng `CliRunner`; `--profile` ngoài repo + `check` không ghi file nào vào repo (test so `git status` trước và sau) |
| M0-13 | `HANDOFF.md` và `status`: tóm tắt đã làm, đang chờ ai, câu hỏi mở, bước tiếp | E | 08 | `core/state/handoff.py` | Sinh đúng sau mỗi lần dừng; test snapshot |
| M0-14 | MCP server khung (mcp 2.x, stdio): tool `status`, `build`, `check`, `approve`, `config_show` | E | 12, S5 | `src/chipgraph/mcp/` | Test gọi tool qua client MCP; Claude Code thấy tool khi cấu hình |
| M0-15 | Tracing OpenTelemetry: span cho run, rule, check, tool call; exporter `none`/`console`/`otlp`; tắt được bằng `offline` | F | 08 | `core/state/trace.py` | Span có thuộc tính theo GenAI semantic conventions; mặc định `none` |
| M0-16 | `examples/tinysoc`: một SoC nhỏ open-source (2–3 module SV, filelist, Makefile lint bằng Verilator) + profile + test đầu-cuối | F | 09, 10, 12 | `examples/tinysoc/`, `tests/e2e/` | `chipgraph build check:all` pass; test resume đầu-cuối pass |
| M0-17 | Docker image `chipgraph-eda`: OSS CAD Suite khóa theo ngày + Python 3.14 + CLI; workflow publish lên GHCR | F | 12 | `docker/`, `.github/workflows/image.yml` | Image chạy được e2e tinysoc trong CI |
| M0-18 | Profile QSoC mẫu (tài liệu, không phải test CI): `docs/examples/qsoc.chipgraph.yml` dùng `make lint BLOCK=…`, `make check` | C | 12 | `docs/examples/` | Lead chạy thử trên clone QSoC: `chipgraph check` ra kết quả khớp `make check` |
| M0-19 | Phát hành: giữ tên `chipgraph` trên PyPI (bản `0.0.1`), workflow release lên PyPI bằng trusted publishing khi có tag; manifest plugin Claude Code (marketplace) gọi `uvx chipgraph@<version> mcp` | F | 01, 14 | `.github/workflows/release.yml`, `plugin/.claude-plugin/` | Tag thử trên TestPyPI cài được bằng `uvx`; `/plugin install` từ repo chạy được MCP server đúng version |

**Thoát M0 khi:** tinysoc e2e xanh trong CI; QSoC chạy được `check` qua engine (M0-18); kill
rồi resume đúng chỗ; import-linter xanh.

---

## 4. M1 — Hiểu (chưa sinh code)

**Mục tiêu:** Design Model của dự án thật, check chéo, và các tính năng dùng hằng ngày
(`/ask`, `/triage`, `/trace`, sơ đồ).

| ID | Task | Lane | depends | writes | accept |
|---|---|---|---|---|---|
| M1-01 | Schema lõi Design Model (DESIGN 4.2): thực thể + quan hệ, pydantic + JSON Schema; store SQLite (tái tạo được) có FTS5 | C | M0-02 | `core/model/` | Nạp và truy vấn round-trip; xóa cache rồi `ingest` lại cho kết quả như cũ |
| M1-02 | API truy vấn: `block`, `module`, `find`, `trace`, `impact` (đọc), `neighbors`; mở qua MCP (`model.*`) | C+E | 01, M0-14 | `core/model/query.py`, `mcp/` | Test truy vấn trên tinysoc; MCP trả JSON đúng schema |
| M1-03 | Format adapter `qsoc-contract` (đọc `util/qsoc_contract.yml`) và `chip-yaml` (schema `chip.yml` của chipgraph) | C | 01 | `adapters/format/` | Nạp contract của QSoC (fixture copy vào test) ra đúng số block, địa chỉ, interrupt |
| M1-04 | Extractor RTL bằng pyslang: module, port (hướng, độ rộng), param, instance, clock/reset (theo luật tên trong profile), FSM (cơ bản) | C | 01, S2 | `adapters/tool/pyslang.py` | Test trên tinysoc và RTL mẫu; wrapper có AUTO của emacs được parse đúng |
| M1-05 | Extractor MAS Markdown deterministic: REQ-ID (hai chế độ, D37), bảng port và thanh ghi theo template, open items | C | 01 | `src/chipgraph/packs/spec_core/` (`pack.yml`, `extract/`), `examples/tinysoc/doc/specs/`, entry point trong `pyproject.toml` | Trích đúng REQ và bảng từ MAS mẫu; lỗi template chỉ rõ dòng; chế độ suy ra cho key không đổi khi đánh số lại |
| M1-06 | `chipgraph ingest`: chạy mọi extractor, dựng model, báo cáo thống kê | C | 03, 04, 05 | `cli/`, `core/model/ingest.py` | Chạy trên tinysoc và (thủ công) QSoC |
| M1-07 | Check chéo: `spec_schema`, `cross_chip` (trùng địa chỉ, đếm interrupt, clock domain), `ports_diff`, `trace` (REQ không có test), `connect` (top và contract), `hardcode`, `duplicate` | C | 06 | `src/chipgraph/checks/` | Mỗi check có ca pass và ca fail cài sẵn trong tinysoc |
| M1-08 | Check `naming` dựa trên pyslang, luật dạng dữ liệu; `src/chipgraph/orgs/qnsc/naming-v1.yml` dịch từ QNSC Naming Rule V1.0 | C | 04 | `src/chipgraph/checks/naming.py`, `src/chipgraph/orgs/qnsc/` | Kết quả trên RTL QSoC khớp `flow/lint/naming_check.py` (lead so thủ công) |
| M1-09 | `gen:diagram`: block diagram, memory map, interrupt map, cây clock/reset → SVG + drawio, deterministic | C | 02 | `src/chipgraph/packs/spec_core/gen/diagram/` | Cùng model thì ra cùng file (byte-identical); test snapshot |
| M1-10 | LLM provider cho **runtime API**: `anthropic`, `anthropic-compatible` (GLM), đếm token và tiền, budget, retry có backoff, chặn nhãn `nda` | D | M0-03, S1 | `adapters/llm/` | Test với provider giả; chặn `nda` có test; budget vượt thì dừng |
| M1-11 | **Runtime `claude-code` (mặc định):** MCP tool `next_task`, `get_context`, `submit`; plugin lặp theo engine; định nghĩa subagent cho từng vai (tool, model); hook chặn ghi ngoài `outputs`; `submit` kiểm diff so với `outputs`; chặn nhãn `nda` trước khi đưa context | D+E | M0-14, S7 | `adapters/runtime/claude_code/`, `plugin/agents/`, `plugin/hooks/`, `core/runtime/` | Trong Claude Code thật: một rule agent chạy xong một task trên tinysoc; ghi ngoài `outputs` bị chặn (test hook); không cần API key |
| M1-11b | Runtime `claude-agent-sdk` (API key) + sandbox (container; OpenShell nếu S4 tốt), dùng cho CI và evals | D | 10, S4 | `adapters/runtime/agent_sdk/` | Cùng task như M1-11 cho cùng kết quả check; agent không ghi được ngoài `outputs`; mạng bị chặn trừ khi cho phép |
| M1-12 | `decide()`: luật → model nhỏ (structured output; ở runtime `claude-code` là subagent model nhỏ) → model lớn theo ngưỡng; log mọi quyết định | D | 11 | `core/engine/decide.py` | Test đủ ba đường; ngưỡng cấu hình được |
| M1-13 | `/ask`: hỏi đáp dùng model query + FTS5 trên tài liệu, **bắt buộc trích dẫn** (file:dòng hoặc model key); không có nguồn thì nói không biết | D+E | 02, 11 | `src/chipgraph/packs/assist/ask/` | Evals nhỏ: 20 câu trên tinysoc, ≥90% có trích dẫn đúng; 0 câu bịa khi không có dữ liệu |
| M1-14 | `/triage`: đọc log lint/sim fail, phân loại bằng `decide()` (infra/RTL/TB/spec), tóm tắt, gợi ý | D+E | 12 | `src/chipgraph/packs/assist/triage/` | Test với 15 log mẫu đã gán nhãn; đúng ≥80% |
| M1-15 | `chipgraph agents-md`: sinh nội dung `AGENTS.md` từ profile và luật org; **tùy chọn**; mặc định in ra hoặc mở PR; chỉ quản lý đoạn giữa `chipgraph:begin/end`, không ghi đè phần của team | E | 04 | `src/chipgraph/packs/spec_core/gen/agents_md/` | Sinh lại không diff; file có sẵn của team giữ nguyên ngoài đoạn được quản lý (test) |
| M1-16 | Plugin Claude Code v0: `/ask`, `/status`, `/trace`, `/triage`, `/init-chipgraph`; cấu hình MCP; marketplace | E | 13, 14, M0-14 | `plugin/` | Cài từ repo bằng `/plugin marketplace add`; lệnh chạy được |
| M1-17 | Khung evals (Inspect AI): chạy bằng `chipgraph eval`, model giả trong CI; job evals thật chạy bằng runtime `claude-agent-sdk` hoặc `claude-code` headless, tài khoản riêng, có trần chi phí | F | 13, 14 | `evals/`, `.github/workflows/evals.yml` | Báo cáo evals lưu thành artifact CI |
| M1-18 | Finding store và waiver: contract `Finding` (DESIGN 4.8); finding lưu ở **state backend** (không vào repo); waiver là một quyết định trong `.chipgraph/decisions/`, gắn hash; lệnh `chipgraph findings`, `waive` | A+E | M0-02, M0-11 | `core/contracts/finding.py` (task này được phép thêm contract), `core/state/findings.py`, `cli/` | Waiver hết hiệu lực khi hash đổi; mọi check ghi finding đúng schema |
| M1-19 | CDC cấu trúc trên Design Model: tín hiệu nối giữa hai clock domain mà không qua cell đồng bộ khai báo trong profile (ví dụ `qnsc_sync`) | C | 04, 07 | `src/chipgraph/checks/cdc_struct.py` | Bắt được ca cài sẵn trong tinysoc; không báo nhầm ca có bộ đồng bộ |
| M1-20 | `/audit`: chạy mọi check deterministic trên toàn dự án, gom finding theo lớp và mức. **Critic được thêm vào audit ở M3-10** (sau khi có vai và bằng chứng bắt buộc) | D+E | 07, 18 | `src/chipgraph/packs/assist/audit/` | Trên tinysoc có lỗi cài sẵn ở lớp 1, 4, 5: tìm ra hết; báo cáo gom đúng lớp |
| M1-21 | `chipgraph learn`: suy ra layout, naming (qua pyslang), header, kiểu filelist, template tài liệu từ repo có sẵn; xuất profile nháp kèm thống kê độ phủ. Kèm `chipgraph try`: dùng profile nháp đó, chỉ chạy lệnh đọc (`ingest` vào cache, `audit`, `ask`), không ghi file nào vào repo; `init --from-learn` ghi `.chipgraph.yml` | C | 04, 08 | `src/chipgraph/learn/` | Trên tinysoc và 2 repo open-source (ví dụ một IP của OpenTitan, một IP của PULP): profile nháp chạy `check` ra ít cảnh báo hơn 5% số file |
| M1-22 | Template Jinja2 chia block, ghi đè từng phần, thứ tự tìm block → dự án → tổ chức → pack | E | M0-04 | `core/config/templates.py` | Ghi đè một block không làm đổi phần còn lại (test snapshot) |
| M1-23 | Plugin cục bộ của dự án (`.chipgraph/plugins/`) nạp qua plugin API; tắt được bằng cấu hình tổ chức | A | M0-03 | `core/plugin_api/local.py` | Plugin mẫu đổi được luật layout; `config check` liệt kê plugin cục bộ đang chạy |
| M1-24 | `chipgraph baseline` (DESIGN 6.4): liệt kê artifact trên nhánh chính kèm hash và lịch sử merge; lead xác nhận; ghi quyết định `baseline`; finding có sẵn được ghi nhận, không chặn build | A+E | M0-11, 06 | `core/engine/baseline.py`, `cli/` | Trên tinysoc có MAS sẵn: sau `baseline`, `build rtl:<block>` không dừng ở gate spec; sửa MAS thì gate về "chờ" |

**Thoát M1 khi:** `ingest` + `baseline` + check chéo chạy trên QSoC (lead xác nhận); `/ask` và `/triage`
đạt ngưỡng evals; plugin cài được.

---

## 5. M2 — Build một block

**Mục tiêu:** agent sinh RTL và testbench độc lập, có vòng sửa, fan-out, review, và mở PR.

| ID | Task | Lane | depends | writes | accept |
|---|---|---|---|---|---|
| M2-01 | Hệ vai và skill: 5 vai (DESIGN 5.1) dạng dữ liệu; skill loader; hạng model và leo thang | D | M1-11 | `core/runtime/roles/`, `src/chipgraph/packs/*/skills/` | Test chọn vai, nạp skill, leo hạng |
| M2-02 | Rule loại `agent` đầy đủ: vòng ghi → check ngay → triage → sửa trong budget; gắn **nhãn lỗi** (context/constraint/verification/planning/infra); trả `AgentResult` | D | 01, M0-08, M1-12 | `core/engine/agent_rule.py` | Test với runtime giả: pass, fail rồi sửa, hết budget thì dừng và viết HANDOFF |
| M2-03 | Rule Planner: block → plan (module, REQ, interface) → node động có giới hạn; engine kiểm plan (tập ghi, phụ thuộc, budget) trước khi chạy | D | 02 | `src/chipgraph/packs/digital_rtl/rules/plan.yml` + code | Plan sai (ghi trùng) bị từ chối |
| M2-04 | Rule `rtl_module` + skill `lang-sv/rtl`, `org/naming`; input chỉ là slice Design Model + REQ + `style.exemplars` của dự án; formatter của dự án chạy sau khi sinh | D | 02, 03 | `src/chipgraph/packs/digital_rtl/` | Trên tinysoc: sinh module mới qua lint và naming; đổi exemplars thì phong cách đổi theo (so bằng check style) |
| M2-05 | Adapter `edalize` (sim Verilator, Icarus) + runner cocotb; parser kết quả cocotb | B | S3 | `adapters/tool/edalize.py` | Chạy testbench cocotb mẫu trên CI Linux |
| M2-06 | Rule `tb_module` (cocotb) + skill `dv/cocotb`: **chỉ thấy spec và interface, không thấy RTL** | D | 02, 05 | `src/chipgraph/packs/dv/` | Test quyền: runtime không cấp file RTL cho vai viết TB |
| M2-07 | Fan-out: workspace riêng cho mỗi nhánh qua VCS adapter (git worktree), kiểm tập ghi (F2), merge theo thứ tự id, xung đột là lỗi, **so giả định** (F4), join có check tích hợp (F5) | A | M0-05, 02 | `core/engine/fanout.py` | Test: hai nhánh độc lập merge sạch; giả định lệch thì dừng hỏi |
| M2-08 | Best-of-N tùy chọn: chọn bằng tool (pass → ít cảnh báo → area) | A | 07 | `core/engine/select.py` | Test chọn đúng bản |
| M2-09 | Critic review: context mới, input là diff + spec + model; output là nhận xét có cấu trúc | D | 01 | `src/chipgraph/packs/digital_rtl/rules/review.yml` | Evals: bắt được ≥ N lỗi cài sẵn trong diff mẫu |
| M2-10 | Review adapter `github`: mở PR, đọc trạng thái review làm gate `pr`; không có quyền merge | B | M0-11 | `adapters/review/github.py` | Test với API giả; e2e thủ công trên repo thử |
| M2-11 | Target `rtl:<block>` nối M2-03…M2-10; lệnh `/plan`, `/rtl` trong CLI và plugin | E | 03–10 | `src/chipgraph/packs/digital_rtl/targets/`, `plugin/` | e2e tinysoc: thêm một block từ MAS có sẵn, ra PR xanh |
| M2-12 | Rule `wrapper` cho IP vendor theo kiểu emacs AUTO (agent viết `AUTO_TEMPLATE`, generator chạy emacs) | D | 04 | `src/chipgraph/packs/digital_rtl/wrapper/` | Trên tinysoc có IP vendor mẫu: wrapper qua `wrap-check` |
| M2-13 | Evals cho `rtl_module` + `tb_module`: bài lấy từ block đã xong (INTMAP, PWM của QSoC, lưu dạng fixture open-source) | F | 11 | `evals/rtl/` | Có số pass@1, số lượt sửa, chi phí |
| M2-14 | **Pilot QSoC**: TIMER hoặc một module con SYSDBG, chạy trên clone QSoC | — | 11 | (repo QSoC) | PR trong QSoC qua CI QSoC không sửa tay; lead ghi bài học vào `docs/pilots/` |

---

## 6. M3 — Spec và thay đổi

| ID | Task | Lane | depends | accept |
|---|---|---|---|---|
| M3-01 | Schema `<ip>.yml` và `chip.yml`; template MAS có mẫu câu EARS | C | M1-01 | Validate được MAS mẫu; lỗi rõ ràng |
| M3-02 | `/mas <ip>`: Author (L2) + Critic sinh câu hỏi; người trả lời; open item chặn gate spec | D | M2-01 | Phiên mẫu trên tinysoc ra MAS và `<ip>.yml` qua `spec-check` |
| M3-03 | SystemRDL + PeakRDL: rule `gen:regblock`, `gen:cheader`, `gen:regdoc` | B+C | M1-01 | Từ `.rdl` mẫu sinh ra RTL, header, HTML; RTL qua lint |
| M3-04 | `/impact`: đi graph và model, chia 3 nhóm (tự sinh lại / cần làm lại / không ảnh hưởng), in bảng | A+C | M1-02 | Test trên tinysoc: đổi một địa chỉ thì bảng đúng |
| M3-05 | `/change`: tạo change request, sửa ở tầng nguồn, gate C, rồi chạy lại phần stale và toàn bộ check liên quan | A | 04 | e2e: change trên tinysoc qua đủ vòng |
| M3-06 | Đóng băng theo giai đoạn: `freeze` trong profile, người duyệt cấp cao | A | 05 | Đổi tầng đã đóng băng mà thiếu người duyệt thì bị chặn |
| M3-07 | **Replay "bỏ GPIO3"** của QSoC: fixture trước và sau; bảng `/impact` so với thay đổi thật | C | 04 | Khớp danh sách file team đã sửa (lead xác nhận) |
| M3-08 | Chuẩn hóa REQ: EARS → dạng có cấu trúc (sự kiện, điều kiện, đối tượng, hành động); Author trích, người duyệt | C+D | 01, 02 | Trích đúng ≥90% REQ trên bộ MAS mẫu |
| M3-09 | Phát hiện mâu thuẫn trong spec: so cặp REQ cùng đối tượng; ràng buộc số giải bằng z3 (chia clock, baud, căn địa chỉ, độ sâu FIFO) | C | 08 | Bộ 20 mâu thuẫn cài sẵn: tìm ≥ 85%, báo nhầm ≤ 10% |
| M3-10 | Critic có bằng chứng bắt buộc: loại mọi finding không có file:dòng hoặc model key; không bao giờ ở mức `error`; thêm Critic vào `/audit` | D | M2-09, M1-18, M1-20 | Test: finding không có bằng chứng bị loại; `/audit` có phần Critic ở mức `warning`/`question` |
| M3-11 | Workspace: manifest `chipgraph-workspace.yml` (ở repo hub hoặc repo manifest riêng), `workspace init/add/sync`, ref khóa, `readonly`; adapter đọc git submodule, Bender, FuseSoC, manifest vendor kiểu QSoC | A+B | M0-05 | `examples/tinysoc-multi/` (3 repo mẫu): `sync` dựng đúng workspace; đọc được Bender.yml và `.core` mẫu |
| M3-12 | Design Model liên repo: fact có nguồn (repo, path, commit); check chéo liên repo; cảnh báo lệch version (ref khóa cũ hơn bản IP đã đổi interface) | C | 11, M1-07 | Lệch port giữa IP và top ở hai repo được bắt; lệch version được báo |
| M3-13 | Change liên repo: một change ID, nhiều PR theo thứ tự phụ thuộc, gate đóng khi mọi PR merge; tôn trọng quyền từng repo | A+B | 11, 12, 05, M2-10 | e2e trên `tinysoc-multi`: thêm một interrupt ở IP ra 3 PR liên kết đúng thứ tự |
| M3-14 | Profile trỏ tới workspace từ xa (`workspace: git+…@ref`): repo IP đọc contract và luật chung mà không clone cả workspace | C | 11 | Repo IP chạy `check` dùng contract từ hub từ xa |

---

## 7. M4 — Verify sâu và đo

| ID | Task | Lane | accept |
|---|---|---|---|
| M4-01 | Rule `sva` + adapter SymbiYosys; parser counterexample | B+D | Property mẫu pass, property sai ra counterexample |
| M4-02 | Vòng coverage: thu coverage (Verilator/cocotb), Author thêm test tới mục tiêu hoặc hết budget | D | Tăng coverage trên block mẫu, có trần chi phí |
| M4-03 | `chipgraph report`: chỉ số DESIGN mục 9, phân bố nhãn lỗi | E | Báo cáo từ journal thật |
| M4-04 | `chipgraph site`: trang tĩnh (ma trận block × stage, truy vết, tác động) | E | Build ra HTML tĩnh; không cần server |
| M4-05 | Evals chạy hằng đêm có trần chi phí; so model (Claude và GLM) theo vai | F | Có bảng so sánh theo vai |
| M4-06 | Tài liệu Langfuse self-host (docker compose) + exporter OTLP | F | Trace hiện trong Langfuse |

---

## 8. M5 — Tổng quát và on-prem

| ID | Task | Lane | accept |
|---|---|---|---|
| M5-01 | Preset `ip-block` + ví dụ IP trên Sky130 open-source | C | Chạy M1–M2 trên ví dụ đó mà không sửa core |
| M5-02 | Format adapter `opentitan-hjson` | C | Nạp một IP OpenTitan vào model |
| M5-03 | Runner `ssh`, `lsf` (+ `slurm`), mục `env` (Environment Modules, biến license); `doctor` kiểm tra | B | Chạy check qua ssh trên máy thử |
| M5-04 | VCS adapter `perforce` (tối thiểu) và review adapter `gitlab` | B | Test contract của adapter |
| M5-05 | Runtime `generic`: vòng agent tự viết, API tương thích OpenAI, tool calling, MCP client, giới hạn quyền | D | Chạy evals M1/M2 với model tự host (sau S6) |
| M5-06 | Gói cài offline (wheelhouse + pack + image) và `offline: true` | F | Cài và chạy trong mạng không có internet (máy thử) |
| M5-07 | Researcher có nguồn; cấu hình nguồn được phép | D | Đề xuất luôn có nguồn; tắt khi offline |
| M5-08 | Dashboard web v0 (đọc state, trả lời gate) | E | Duyệt gate trên web ghi đúng approval |

---

## 9. Chiến lược test

| Tầng | Công cụ | Chạy khi |
|---|---|---|
| Unit | pytest | Mọi PR |
| Contract adapter | Bộ test chung cho mỗi loại adapter (cùng input, đúng schema) | Mọi PR đụng adapter |
| Golden và snapshot | Sơ đồ, `AGENTS.md`, HANDOFF, report | Mọi PR |
| Đầu-cuối | `examples/tinysoc` trong Docker `chipgraph-eda` | Mọi PR (model giả) |
| Resume | Kill ở từng loại bước | Mọi PR đụng engine |
| Evals | Inspect AI, model thật, trần chi phí | Hằng đêm, và PR có nhãn `evals` |
| Pilot | Clone QSoC | Thủ công, theo mốc |

**Model giả:** `adapters/llm/fake.py` trả lời theo kịch bản ghi sẵn, để CI không tốn tiền và
kết quả tất định.

---

## 10. Định nghĩa "xong" cho mọi task

- [ ] Chỉ sửa file trong `writes`; không đổi contract ngoài phạm vi.
- [ ] Có test cho mọi `accept`; CI xanh (ruff, mypy, pytest, import-linter).
- [ ] Hàm và lớp công khai có docstring; lệnh mới có `--help`.
- [ ] Cập nhật tài liệu liên quan (README của pack, `docs/`); quyết định mới thì thêm vào
      `DECISIONS.md` và cần lead duyệt.
- [ ] Không có secret, dữ liệu nội bộ hay dữ liệu NDA trong repo.
- [ ] PR mô tả: task ID, đã làm gì, cách kiểm, rủi ro còn lại.

---

## 11. Thứ tự đề xuất (tuần tương đối)

```
Tuần 1   M0-01 ─┬─ S1 S2 S3 S4 S5 (song song)
                └─ M0-02 ─ M0-03 ─┬─ M0-04 ─ M0-10
                                  ├─ M0-05 ─ M0-06 ─┐
                                  ├─ M0-07 ─────────┴─ M0-08 ─ M0-11 ─ M0-12 ─┬─ M0-13
                                  └─ M0-09                                     ├─ M0-14
Tuần 3                                                                         ├─ M0-15
                                                                               └─ M0-16 ─ M0-17 ─ M0-18
Tuần 4+  M1: lane C (01→07, 08, 09) ∥ lane D (10→11→12→13, 14) ∥ lane E (15, 16) ∥ F (17)
Sau M1   M2 → M3 → M4 → M5 (mỗi mốc có demo và lead duyệt trước khi sang mốc sau)
```

Số tuần chỉ để xếp thứ tự, không phải cam kết. Mỗi mốc kết thúc bằng **demo cho lead** và
cập nhật `DESIGN.md` nếu thực tế khác thiết kế.
