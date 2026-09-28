# qnsc-icagent — Design và Plan

| Mục | Giá trị |
|---|---|
| Tên tạm | `qnsc-icagent` (sẽ chốt sau) |
| Chủ sở hữu | Trong Nghia (Nghia VT) |
| Trạng thái | **Nháp v0.1, để thảo luận** — chưa có code |
| Ngày | 2026-09-26 |
| Dự án áp dụng đầu tiên | QSoC (`vlsi_deep_training`) |

---

## 0. Tóm tắt: đây là gì

**`qnsc-icagent` là một phần mềm: hệ thống agent AI cho thiết kế chip.** Người kỹ sư ra
lệnh bằng ngôn ngữ tự nhiên; tool chia việc cho các agent AI, cho tool EDA kiểm, và dừng
lại để người duyệt ở các mốc quan trọng.

Nó **không phải** một skill. Skill chỉ là một phần bên trong:

| Thành phần | Là gì | Ví von |
|---|---|---|
| CLI `icagent` + orchestrator | Chương trình Python điều phối | Quản đốc xưởng |
| Agents | AI làm từng việc (viết spec, code, test, debug, review) | Thợ |
| Skills | Hướng dẫn cách làm một loại việc (naming rule, viết wrapper…) | Sổ tay nghề của thợ |
| MCP tools | Lint, sim, synth, formal… | Dụng cụ đo |
| Gates | Chỗ người duyệt | Kiểm tra chất lượng (QC) |
| Pack, adapter, profile | Kiến thức và luật theo lĩnh vực, theo dự án | Bộ khuôn cho từng loại sản phẩm |
| Plugin Claude Code, dashboard | Nơi người dùng gõ lệnh và theo dõi | Bàn điều khiển |

Giao cho người dùng: một **CLI** + một **plugin Claude Code** (có commands, agents, skills,
MCP) + sau này một **dashboard web**. Dùng cho mọi dự án chip bằng một file
`.icagent.yml`.

---

## 1. Mục tiêu

Xây một **AI tool độc lập** cho team dùng trong mọi dự án chip/IC, không riêng QSoC.
Tool đi cùng người kỹ sư từ ý tưởng sản phẩm đến RTL đã kiểm chứng, và sau đó mở rộng
sang verification, backend, firmware, quản lý dự án.

**Một câu:** AI soạn, code kiểm, người duyệt, ở mọi tầng từ PRD đến PR.

### 1.1 Phạm vi

| Có | Không |
|---|---|
| Soạn và kiểm spec (PRD, HAS, MAS, contract) | Train hoặc fine-tune model riêng |
| Sinh RTL, wrapper, testbench, SVA, có vòng kiểm bằng tool | Thay thế tool EDA (tool chỉ gọi EDA) |
| Điều phối nhiều agent theo quy trình cố định có gate | Agent tự trị không có người duyệt |
| Dùng cho nhiều dự án qua file profile | Gắn cứng vào một chip, một PDK, một naming rule |
| Đo chất lượng bằng evals | Đánh giá bằng cảm nhận |

### 1.2 Độc lập với flow của thầy

Flow `VLSIT_RTL_Generator_AI_Model` của thầy là **nguồn ý tưởng** (gate, RTM, spec
parser). `qnsc-icagent` **không dùng lại file, prompt hay code nào** từ repo đó. Mọi thứ
được viết mới, là giải pháp riêng của dự án này.

---

## 2. Nguyên tắc thiết kế

| # | Nguyên tắc | Nghĩa là |
|---|---|---|
| P1 | **AI soạn, code kiểm, người duyệt** | Không output nào của AI được dùng khi chưa qua check tự động và người chốt |
| P2 | **Tool là trọng tài** | "Xong" = lint/sim/formal/check pass, không phải agent tự báo xong |
| P3 | **Code điều phối, agent làm việc nhỏ** | Orchestrator là chương trình có máy trạng thái; agent không điều khiển agent |
| P4 | **Task là hợp đồng** | Mỗi task có input, output, tiêu chí nghiệm thu, ngân sách lượt thử |
| P5 | **Độc lập giữa người viết và người kiểm** | Tester và Coder cùng đọc spec nhưng không thấy bài nhau; Reviewer dùng context mới |
| P6 | **Mọi trạng thái nằm trong file** | Agent không nhớ gì; dừng/tiếp tục/chạy lại được; mọi thứ truy vết được |
| P7 | **Số liệu ở dạng máy đọc, sinh ra thay vì viết tay** | Địa chỉ, port, thanh ghi, interrupt nằm trong YAML/RDL; package, header, bảng, nối dây top được sinh ra |
| P8 | **Lõi không biết gì về chip cụ thể** | Loại dự án, ngôn ngữ, bus, tool, PDK, luật, quy trình đều là pack, adapter hoặc dữ liệu (mục 5) |
| P9 | **Hết ngân sách thì báo người** | Không lặp vô hạn; mọi câu hỏi mở chờ người trả lời |
| P10 | **Dùng lại cái dự án đã có** | Đọc format spec và flow `make` sẵn có qua adapter, không bắt dự án viết lại |

---

## 3. Bối cảnh: đã có gì trên thế giới

| Nhóm | Ví dụ | Học được gì |
|---|---|---|
| Agent sinh RTL có vòng lặp | MAGE, VerilogCoder, AutoChip | Cần tool phản hồi và agent debug |
| AI cho verification | Spec2Cov, LLM4Cov, AgentDV, AssertLLM, ChatSVA | Sinh test/SVA từ spec, lặp theo coverage |
| Benchmark | NVIDIA CVDP (~34% pass@1 cho bài thực tế) | Việc thật vẫn cần kiểm chứng và người duyệt |
| Plugin/flow cho Claude Code | Gateflow Plugin, VeriFlow-CC | Hình dạng plugin (agents, skills, commands) khả thi |
| MCP cho EDA | MCP4EDA, mcp4eda, ChipAgent | Đóng gói tool EDA cho AI gọi |
| Chuẩn hóa spec | SystemRDL, IP-XACT, OpenTitan topgen/reggen/tlgen, EARS | Spec máy đọc được; top/crossbar/interrupt sinh ra từ dữ liệu |
| Điều phối agent | Anthropic "Building effective agents", MetaGPT, LangGraph, MAST | Workflow cố định trước; hệ nhiều agent hỏng vì giao việc mơ hồ, lệch nhau, không kiểm chứng |
| Thương mại | Synopsys, Cadence ChipStack, Siemens | Hướng agentic là xu hướng chính, nhưng đóng và có license |

**Khoảng trống:** chưa có tool open nào ghép đủ **spec model + điều phối có gate + tool EDA
+ profile nhiều dự án + kho kiến thức tích lũy**. Đó là chỗ của `qnsc-icagent`.

### 3.1 Các công ty lớn đang làm gì (research 2026-09-27)

| Công ty | Làm gì | Ý chính |
|---|---|---|
| **Cadence** ChipStack AI Super Agent | Đọc spec, RTL, tài liệu, rồi dựng một **"Mental Model"**: mô hình có cấu trúc về ý đồ thiết kế, phân cấp, quan hệ; kết hợp phân tích tĩnh và LLM. Mọi agent **bắt buộc tham chiếu** mô hình này khi sinh code hay test. Rồi chạy vòng test plan, test, sim/formal, phân tích | Nguồn sự thật dùng chung chống ảo giác; công bố tới 10x ở front-end |
| **Siemens** Fuse EDA AI Agent, Questa One Agentic Toolkit | Agent lập kế hoạch và điều phối nhiều tool; **"self-verifying"**: mỗi bước đi qua engine EDA deterministic để kiểm trước khi flow đi tiếp | Kiểm từng bước, không chỉ kiểm cuối |
| **Synopsys** AgentEngineer, Synopsys.ai Copilot | Các mức tự động L1–L5 (đã có L4 cho design và verification); agent verification chạy dài; agent tối ưu QoR ở implementation; copilot trong tool (hỏi đáp, sinh script) | Tự động hóa theo bậc; copilot là bước đầu, agent là bước sau |
| **NVIDIA** ChipNeMo | LLM riêng cho chip; 3 ứng dụng nội bộ có giá trị: **chatbot trợ lý kỹ sư, sinh script EDA, tóm tắt và phân tích bug** | Việc "nhỏ" nhưng dùng hằng ngày mang lại giá trị lớn; retrieval theo lĩnh vực rất quan trọng |
| **NVIDIA** Marco | Framework nhiều agent, task là **đồ thị cấu hình được, tĩnh hoặc động**; VerilogCoder, RTLFixer | Giống workflow dạng dữ liệu + DAG của mình, thêm phần đồ thị động |
| **NVIDIA** Agent Toolkit: OpenShell | Runtime **open-source** chạy agent (kể cả Claude Code, không cần sửa) trong sandbox theo chính sách khai báo, **log lại mọi quyết định cho phép hay chặn** | Không cần tự viết sandbox |
| **Google** AlphaChip | Học tăng cường (RL) để đặt macro/floorplan, dùng cho 3 thế hệ TPU; open-source `circuit_training` | Bài toán tối ưu dùng RL hoặc tìm kiếm, không dùng LLM |
| **Startup** ChipAgents (120+ triển khai), Bronco, Cognichip, Agentrys | Tập trung vào **verification, debug sim, đóng coverage**; Agentrys tập trung vào điều phối và kiểm soát doanh nghiệp | Tiền và nhu cầu đang dồn vào verification và debug |
| **Cộng đồng** EDA_MCP | MCP server nối agent chạy trên máy cá nhân với **cụm máy Linux có EDA thương mại qua SSH** | Tool thương mại nằm trên server có license, agent phải gọi từ xa |

### 3.2 Đề xuất áp dụng vào design (chờ chốt)

| # | Ý tưởng | Học từ | Thay đổi trong design | Mức |
|---|---|---|---|---|
| I1 | **Design Model**: nâng spec model thành một mô hình tri thức chung, gồm spec (mục 6) + dữ kiện trích từ RTL (phân cấp, port, clock, reset, thanh ghi, FSM) + quan hệ giữa chúng. Agent hỏi qua tool `model.query`, không đọc file thô | Cadence Mental Model | Mục 6 mở rộng; thêm `icagent ingest` để dựng model từ dự án có sẵn (spec + RTL) | **Migrate**: tốt hơn cách cũ |
| I2 | **Kiểm từng bước**: mỗi bước của agent (không chỉ cuối task) phải qua check deterministic trước khi sang bước sau | Siemens self-verifying | Mục 7.3: thêm check sau mỗi bước sinh file | Siết lại P2 |
| I3 | **Đồ thị task động có giới hạn**: Planner được đề xuất thêm hoặc tách node lúc chạy; code kiểm đồ thị mới (tập ghi, phụ thuộc, ngân sách) trước khi chạy | NVIDIA Marco | Mục 7.2, 7.5 | Mở rộng |
| I4 | **Dùng OpenShell (hoặc container) làm sandbox** thay vì tự viết; lấy audit trail có sẵn | NVIDIA OpenShell | Mục 14.3 "Giới hạn và sandbox" | **Reuse** |
| I5 | **Chế độ Ask**: hỏi đáp về spec, luật, IP, dự án cũ, **luôn kèm trích dẫn**. Thêm **tóm tắt lỗi sim/lint** và **sinh script EDA** (Tcl cho DC/PT, script sim) có chạy thử | NVIDIA ChipNeMo, Synopsys Copilot | Lệnh `/ask`, `/triage`, `/script`; đưa lên v0.1–v0.2 vì rẻ và dùng hằng ngày | Thêm, **ưu tiên sớm** |
| I6 | **Adapter chạy từ xa**: tool EDA chạy qua SSH hoặc job scheduler (LSF, Slurm) trên server có license | EDA_MCP | Mục 5.4: adapter có `runner: local \| ssh \| lsf \| slurm` | Thêm |
| I7 | **Dùng thang L1–L5** của ngành khi nói về mức tự động, map với mức 0–3 của mình | Synopsys | Mục 12.2 | Đổi cách gọi |
| I8 | **Pack tối ưu không dùng LLM**: dò tham số synth/PnR bằng tìm kiếm Bayes hoặc RL; LLM chỉ đề xuất không gian tham số và đọc kết quả | Synopsys DSO.ai, Google AlphaChip | Pack `optimize` cho backend, sau v1.0 | Để sau |
| I9 | **Ưu tiên verification và debug** trong lộ trình | ChipAgents, Bronco, Synopsys | Đưa Tester, SVA và triage lên sớm hơn | Sắp lại lộ trình |

**Không áp dụng lúc này:**
- Tự train model riêng (ChipNeMo): tốn công, model frontier đã đủ mạnh. Chỉ xem lại nếu
  có dữ liệu lớn và cần chạy local.
- Các sản phẩm đóng của Synopsys, Cadence, Siemens: học ý tưởng, không phụ thuộc.
- Nemotron hay model local: chỉ khi chính sách NDA bắt buộc (mục 14.3).

---

## 4. Kiến trúc tổng thể

```
┌──────────────────────────────────────────────────────────────────────┐
│  Người dùng: /intake /arch /mas /plan /rtl /verify /review ...        │
└───────────────┬──────────────────────────────────────────────────────┘
                ▼
┌───────────────────────────┐    ┌─────────────────────────────────────┐
│ ORCHESTRATOR (code)       │◀──▶│ STATE (file trong repo dự án)       │
│ máy trạng thái · DAG task │    │ .icagent/tasks.yml · logs · RTM     │
│ gate · ngân sách · retry  │    └─────────────────────────────────────┘
└──────┬────────────────────┘
       │ giao task (hợp đồng)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│ AGENTS  Spec Writer · Spec Critic · Architect · Planner · Coder ·     │
│         Tester · Debugger · Reviewer · Reporter                       │
└──────┬───────────────────────────────────────────────┬───────────────┘
       │ gọi tool                                        │ đọc
       ▼                                                 ▼
┌───────────────────────────┐    ┌─────────────────────────────────────┐
│ TOOLS (MCP server)        │    │ KNOWLEDGE BASE                      │
│ spec check · lint · sim · │    │ thư viện IP · dự án cũ · template · │
│ synth · formal · sta ·    │    │ naming rule · checklist             │
│ wave · report parse       │    └─────────────────────────────────────┘
└──────┬────────────────────┘
       ▼
  EDA thật (verilator, verible, slang, yosys, symbiyosys, iverilog, vcs, dc, ...)
       ▲
┌──────┴────────────────────┐    ┌─────────────────────────────────────┐
│ PROFILE .icagent.yml      │    │ EVALS                               │
│ luật riêng của dự án      │    │ bài thi từ block thật · đo % pass   │
└───────────────────────────┘    └─────────────────────────────────────┘
```

| Lớp | Vai trò | Build hay dùng lại |
|---|---|---|
| Orchestrator | Quyết định bước tiếp theo, giao task, giữ gate | Tự build (Python) |
| Agents | Làm từng việc nhỏ có giới hạn | Tự build (prompt + quyền tool) trên Claude Agent SDK |
| Tools (MCP) | Chạy EDA, trả kết quả có cấu trúc | Tự build server; EDA là open-source hoặc thương mại có sẵn |
| Knowledge base | Cho AI dữ kiện đúng, không bịa | Tự build, tích lũy qua các dự án |
| Profile | Làm tool độc lập dự án | Mỗi dự án một file |
| Evals | Đo chất lượng agent | Tự build từ block thật |

---

## 5. Tính tổng quát: một lõi, nhiều dự án

Mục tiêu: dùng được cho **nhiều loại dự án chip/IC**, không chỉ MCU SoC như QSoC, mà
**không phải sửa lõi**. Cách làm: lõi nhỏ không biết gì về chip cụ thể; mọi thứ thay đổi
giữa các dự án là **dữ liệu, pack hoặc adapter**.

### 5.1 Cái gì khác nhau giữa các dự án, và xử lý bằng gì

| Khác nhau | Ví dụ | Xử lý bằng |
|---|---|---|
| Loại dự án | MCU SoC, accelerator, một IP lẻ, FPGA prototype, mixed-signal, analog IP | **Preset** (bộ cấu hình mẫu) + **workflow dạng dữ liệu** |
| Ngôn ngữ thiết kế | SystemVerilog, Verilog, VHDL, Chisel, SpinalHDL, HLS | **Pack** theo ngôn ngữ (skill, luật, parser) |
| Bus, giao thức | APB, AHB, AXI, TileLink, Wishbone, bus riêng | **Thư viện interface dạng dữ liệu**, không viết trong code |
| Tool EDA | Verilator, VCS, Xcelium, Questa, Yosys, DC, Genus, OpenROAD, Innovus | **Tool adapter** sau một giao diện chung |
| Đích | ASIC (GF180, Sky130, TSMC…), FPGA (Xilinx, Lattice…) | Profile + adapter |
| Quy trình, gate | Team 5 người vs team 50 người; có hay không có stage backend | **Workflow dạng dữ liệu** |
| Naming, coding rule | QNSC Naming Rule, lowRISC style, luật công ty khác | **Luật dạng dữ liệu** + checker cắm vào |
| Tài liệu | Markdown, docx, template của công ty | **Template** theo tổ chức |
| Format spec có sẵn | IP-XACT, SystemRDL, OpenTitan hjson, YAML riêng (như `qsoc_contract.yml`) | **Format adapter** (import/export) |
| Model AI | Claude, model khác, model chạy local cho dữ liệu NDA | **LLM adapter** |
| Chính sách dữ liệu | Có NDA hay không, cho gửi cloud hay không | Cấu hình cấp tổ chức, lõi bắt buộc tuân theo |

### 5.2 Bốn lớp: core, pack, adapter, dữ liệu dự án

```
┌──────────────────────────────────────────────────────────────────────┐
│ CORE — không biết gì về chip                                          │
│ orchestrator · task/DAG · gate · state · đồ thị truy vết + hash ·     │
│ agent runtime · luật quyền đọc/ghi · plugin API · LLM adapter API     │
├──────────────────────────────────────────────────────────────────────┤
│ PACKS — kiến thức theo lĩnh vực, cài thêm                             │
│ spec-core · lang-sv · lang-vhdl · digital-rtl · dv · fpga · backend · │
│ firmware · pm · analog (sau)                                          │
├──────────────────────────────────────────────────────────────────────┤
│ ADAPTERS — nối với tool và format cụ thể                              │
│ tool: verilator · vcs · xcelium · questa · yosys · dc · openroad ·    │
│       opensta · primetime · symbiyosys · vivado                       │
│ format: ip-xact · systemrdl · opentitan-hjson · yaml riêng · docx     │
│ llm: claude · khác · local                                            │
├──────────────────────────────────────────────────────────────────────┤
│ DỮ LIỆU DỰ ÁN — mỗi dự án tự có                                       │
│ .icagent.yml · preset · luật · template · spec · kb riêng             │
└──────────────────────────────────────────────────────────────────────┘
```

**Luật phụ thuộc:** core không bao giờ import pack hay adapter. Pack chỉ gọi core qua
plugin API. Không có chữ "QSoC", "APB", "GF180" hay "Verilator" nào trong core.

### 5.3 Pack là gì

Một pack là một thư mục có manifest khai báo nó cung cấp gì và cần gì:

```yaml
# packs/digital-rtl/pack.yml
name: digital-rtl
version: 0.1.0
requires:
  core: ">=0.1"
  packs: [spec-core]
  capabilities: [lint, sim]          # adapter nào cung cấp thì tùy profile
provides:
  schema:    [schema/block_rtl.json]              # mở rộng spec
  agents:    [coder, debugger]
  skills:    [write_rtl, write_wrapper, fix_lint]
  checks:    [ports_diff, naming]
  workflows: [workflows/rtl_block.yml]
  templates: [templates/rtl_module.sv.j2]
  commands:  [/rtl, /wrap]
```

Dự án chỉ bật những pack cần dùng. Dự án FPGA không bật `backend`; IP analog không bật
`digital-rtl`.

### 5.4 Agent xin "khả năng", không xin tool

Agent gọi `sim(block, test)`, không gọi `verilator`. Adapter chuyển thành lệnh thật và
trả về **cùng một kiểu kết quả**:

| Khả năng | Adapter ví dụ | Kết quả chung (JSON) |
|---|---|---|
| `lint` | verilator, verible, spyglass | `{status, issues:[{file, line, rule, msg}]}` |
| `sim` | verilator, iverilog, vcs, xcelium, questa | `{status, tests:[{name, pass, time, log_tail}]}` |
| `formal` | symbiyosys, jaspergold | `{status, props:[{name, result, cex}]}` |
| `synth` | yosys, dc, genus, vivado | `{status, area, cells, warnings}` |
| `sta` | opensta, primetime, tempus | `{status, wns, tns, worst_paths}` |
| `parse` | pyslang, verible | `{modules:[{name, ports, params, instances}]}` |

Adapter cũng có thể chỉ là **một lệnh của dự án** (`make lint BLOCK={block}`) cộng với bộ
đọc log. Cách này giúp dự án có sẵn flow riêng vẫn dùng được ngay.

### 5.5 Quy trình là dữ liệu

Stage, gate và thứ tự không viết cứng trong code. Mỗi workflow là một file:

```yaml
# packs/digital-rtl/workflows/rtl_block.yml
name: rtl_block
stages:
  - id: spec_check
    run: check:spec
  - id: plan
    agent: planner
    gate: plan                     # dừng chờ người
  - id: implement
    foreach: task
    parallel: [agent:tester, agent:coder]
    accept: [lint, naming, sim]
    on_fail: { agent: debugger, max_tries: 3, then: escalate }
  - id: review
    agent: reviewer
    gate: pr
```

Dự án có thể thêm stage (ví dụ CDC check), bỏ gate, hay đổi thứ tự trong profile mà
không sửa pack.

**Preset** là bộ khởi đầu cho từng loại dự án:

| Preset | Pack bật sẵn | Workflow |
|---|---|---|
| `mcu-soc` | spec-core, lang-sv, digital-rtl, dv, firmware, backend, pm | PRD → HAS → MAS → RTL → DV → backend |
| `ip-block` | spec-core, lang-sv, digital-rtl, dv | MAS → RTL → DV |
| `accelerator` | như `mcu-soc`, thêm skill cho datapath, pipeline | như `mcu-soc` |
| `fpga-prototype` | spec-core, lang-sv, digital-rtl, dv, fpga | MAS → RTL → DV → bitstream |
| `analog-ip` (sau) | spec-core, analog | spec → netlist → sim SPICE |

### 5.6 Spec tổng quát

- **Schema lõi** có các thực thể dùng chung cho mọi dự án: `block`, `port`, `interface`,
  `clock`, `reset`, `parameter`, `register`, `interrupt`, `memory_region`, `requirement`,
  `decision`, `open_item`. Pack mở rộng thêm (ví dụ pack analog thêm `pin_spec` với
  gain, bandwidth).
- **Interface là dữ liệu**: APB, AXI, TileLink… được mô tả trong thư viện interface
  (tên tín hiệu, hướng, độ rộng, vai trò master/slave), giống bus definition của IP-XACT.
  Thêm một bus mới là thêm một file, không sửa code.
- **Format adapter**: import/export IP-XACT, SystemRDL, OpenTitan hjson, hoặc YAML riêng
  của dự án. Dự án có sẵn **không phải viết lại spec**: QSoC giữ `qsoc_contract.yml`, tool
  đọc nó qua một adapter.

### 5.7 Cấu hình nhiều tầng

Cấu hình được ghép từ trên xuống, tầng dưới ghi đè tầng trên:

```
mặc định của tool
  └─ tổ chức     (qnsc: naming rule, template tài liệu, chính sách dữ liệu, người duyệt)
       └─ preset (mcu-soc)
            └─ dự án  (.icagent.yml của QSoC)
                 └─ block (ngoại lệ riêng của một block)
```

Nhờ vậy, luật chung của công ty viết một lần; mỗi dự án chỉ khai báo phần khác.

### 5.8 Chứng minh tính tổng quát

| Cách | Làm gì |
|---|---|
| Chạy trên nhiều loại dự án | Tối thiểu 3 loại: QSoC (MCU SoC, SV, GF180); một IP lẻ trên flow open-source Sky130; một dự án FPGA. Thêm một block OpenTitan để thử import format lạ |
| Test cho từng pack và adapter | Mỗi adapter có bộ test chung: cùng input, kết quả đúng kiểu JSON chung |
| Kiểm luật phụ thuộc | CI chặn core import pack/adapter; grep tên dự án, bus, PDK trong core |
| Evals theo nhiều dự án | Bộ bài thi lấy từ nhiều dự án, không chỉ QSoC |

**Không trừu tượng hóa sớm:** các điểm mở rộng (pack, adapter, workflow, preset) có từ
ngày đầu, nhưng chỉ viết adapter hay preset thứ hai khi có dự án thật cần đến. Tránh
xây framework lớn trước khi có người dùng.

---

## 6. Spec model

### 6.1 Chuỗi tài liệu từ đầu

```
0. PRD             ← NGƯỜI nêu mục tiêu, ràng buộc; AI phỏng vấn cho rõ
        ▼
1. HAS + chip.yml  ← AI đề xuất 2–3 kiến trúc, NGƯỜI chọn; AI viết cả hai CÙNG LÚC
        ▼
2. MAS + <ip>.yml + <ip>.rdl   ← AI soạn từng IP, Spec Critic soi, CHỦ IP duyệt
        ▼
3. RTL, verify, ...            ← các agent ở mục 7
```

HAS và `chip.yml` ra đời cùng lúc: contract là phần máy đọc được của HAS.

### 6.2 Ba tầng spec

| Tầng | File | Nội dung |
|---|---|---|
| 1. Chip contract | `spec/chip.yml` (1 file/dự án) | Danh sách IP, memory map, bus, interrupt line, clock/reset domain, pad, `tbd:` |
| 2. IP spec máy đọc | `spec/ip/<ip>.yml`, `spec/ip/<ip>.rdl` | Port + clock domain, tham số, interrupt (level/pulse), bus interface; thanh ghi bằng SystemRDL |
| 3. IP spec chữ | `spec/ip/<ip>_MAS.md` | Hành vi theo mẫu EARS, REQ-ID, open items, lý do thiết kế |

### 6.3 Ba loại thông tin và ai làm

| Loại | Ví dụ | Ai làm |
|---|---|---|
| Quyết định | Chọn IP, tần số clock, nguồn NMI, bỏ một IP | AI đưa phương án + ưu nhược, **người chọn** |
| Dữ kiện có sẵn | Số interrupt, bus, độ rộng port của IP vendor | AI đọc RTL/tài liệu và điền; **code đối chiếu với RTL thật** |
| Suy ra được | Địa chỉ slave, số thứ tự interrupt, độ rộng `paddr` | **Code tính**, không cần AI |

### 6.4 Sinh ra, không viết tay

Từ tầng 1 và 2 sinh ra: package hằng số SystemVerilog, RTL thanh ghi, header C cho
firmware, bảng trong MAS/HAS, phần nối dây của top, tài liệu `.docx`/`.pdf`.

### 6.5 Kiểm bằng code (deterministic)

| Check | Bắt được |
|---|---|
| Schema | Thanh ghi thiếu reset value, port thiếu clock domain, interrupt thiếu kiểu |
| Chéo IP và chip | Trùng địa chỉ, số interrupt IP khác số line trong contract, clock domain không tồn tại |
| Spec và RTL | Port trong `<ip>.yml` khác port RTL thật (parse bằng slang/verible) |
| Truy vết | REQ không có test, RTL không trỏ về REQ |
| Tác động | Đổi một số trong contract thì liệt kê IP, RTL, test, tài liệu bị ảnh hưởng |

### 6.6 Việc của AI ở phần spec

| Agent | Việc |
|---|---|
| Spec Writer | Soạn PRD/HAS/MAS và YAML nháp đúng template |
| Architect | Đề xuất kiến trúc, chọn IP từ thư viện, ước lượng |
| Spec Critic | Tìm chỗ mơ hồ, thiếu; sinh danh sách câu hỏi |
| Cross-IP Reviewer | Tìm lệch nghĩa giữa các IP mà schema không bắt được |

**Luật cứng:** AI không tự trả lời câu hỏi mở. Spec còn open item thì không qua Gate 1.

---

## 7. Điều phối agent

### 7.1 Agents

| Agent | Đọc | Ghi | Tool được gọi |
|---|---|---|---|
| Spec Writer | PRD, HAS, template, KB | `spec/**` | spec check |
| Spec Critic | `spec/**` | danh sách câu hỏi | spec check |
| Architect | PRD, KB (thư viện IP, dự án cũ) | phương án kiến trúc | — |
| Planner | spec đã chốt | `.icagent/tasks.yml` | spec check |
| Coder | spec + task | đúng file output của task | lint, naming |
| Tester | **chỉ spec** (không thấy RTL đang viết) | TB, SVA của task | lint, sim |
| Debugger | log fail, RTL, TB, spec | RTL hoặc TB của task | lint, sim, wave |
| Reviewer | diff + spec, **context mới** | nhận xét | mọi check đọc |
| Reporter | state, log | tracker, báo cáo | — |
| Researcher | KB, nguồn được phép (mục 14.2) | đề xuất có nguồn | tìm kiếm, đọc web |

Checker **không phải agent**: là các tool trong MCP server.

### 7.2 Task là hợp đồng

```yaml
id: T07
block: timer
req: [REQ-004, REQ-005]
input:  [spec/ip/timer_MAS.md#3.2, spec/ip/timer.yml]
output: [design/timer/rtl/m_qnsc_timer_cnt.sv]   # agent chỉ được ghi file này
accept: [lint_clean, naming_clean, sim:tb_timer_cnt]
depends_on: [T05]
budget: { tries: 3, tokens: 200k }
status: todo            # todo | running | check | review | done | blocked
owner_gate: nghia
```

- Chia task theo ranh giới file/module, không theo vai trò chat qua lại.
- Task độc lập chạy song song; task phụ thuộc xếp theo DAG.

### 7.3 Vòng chạy của một block

```
Spec ─▶ Spec Critic ─▶ [Gate 1: người chốt spec]
     ─▶ Planner ─▶ tasks.yml ─▶ [Gate 2: người chốt plan]
     ─▶ với mỗi task:
          Tester (TB/SVA từ spec) ─┐
          Coder  (RTL từ spec)    ─┤
                                   ▼
                     Checker (tool: lint · naming · sim · formal)
                        │ fail                     │ pass
                        ▼                          ▼
              Debugger (≤ budget) ── hết ──▶ báo người
                                             Reviewer (context mới)
                                                   ▼
                                     [Gate 3: người duyệt PR]
```

### 7.4 Kiểm chứng chính các agent

| Cơ chế | Chống lỗi gì |
|---|---|
| Tool quyết "xong" | Agent tự báo xong sai |
| Tester và Coder độc lập | Hai bên cùng sai theo một cách |
| Reviewer context mới | Bị cuốn theo lập luận của Coder |
| Quyền ghi chỉ đúng file output | Agent sửa lan ra ngoài phạm vi |
| Ngân sách + báo người | Lặp vô hạn, tốn token |
| Evals | Đổi prompt/model làm chất lượng tụt mà không biết |

### 7.5 Fan-out: chạy nhiều agent song song

**Có**, nhưng chỉ khi việc tách được thật. Orchestrator (code) quyết định fan-out, không
phải agent.

| Kiểu fan-out | Khi nào | Ví dụ |
|---|---|---|
| **Theo task độc lập** | Các task trong DAG không phụ thuộc nhau, output không chung file | 8 module con của SYSDBG code song song |
| **Theo block** | Nhiều IP cùng làm spec hoặc RTL | `/mas` cho 5 IP cùng lúc, mỗi chủ IP duyệt phần mình |
| **Nhiều bản cho một việc** (best-of-N) | Việc khó, dễ sai; tool chấm được | Sinh 3 bản RTL, giữ bản pass test và ít cảnh báo nhất |
| **Theo góc nhìn review** | Review cần nhiều chuyên môn | Reviewer spec, Reviewer CDC/reset, Reviewer naming chạy song song trên cùng diff |
| **Theo nguồn research** | So nhiều phương án | Mỗi Researcher tìm một phương án kiến trúc |

**Không fan-out khi:**
- các task ghi chung file;
- bước sau cần kết quả bước trước;
- việc quá nhỏ (chi phí khởi động nhiều hơn lợi ích);
- interface giữa các phần chưa chốt.

Có trần số agent chạy cùng lúc, trần chi phí cho mỗi lần fan-out, và giới hạn theo rate
limit của API và license EDA.

### 7.6 Chọn model theo việc để giảm chi phí

Mỗi vai agent có một **hạng model** mặc định, cấu hình được trong profile:

| Hạng | Dùng cho | Ví dụ model hiện tại |
|---|---|---|
| **Nhỏ, rẻ** | Việc máy móc: tóm tắt log, đọc report, sửa lint đơn giản, định dạng, Reporter | Haiku 4.5 |
| **Vừa** | Việc thường: Coder, Tester cho module rõ ràng, Spec Writer | Sonnet 5 |
| **Mạnh** | Việc cần suy luận: Architect, Planner, Spec Critic, Reviewer, Debugger khi lỗi khó | Opus 5.5 |

**Leo thang theo bậc (cascade):** bắt đầu bằng model rẻ nhất đủ dùng; fail thì lượt sau
lên hạng trên; vẫn fail thì báo người.

```
Coder (vừa) ─ fail ─▶ Debugger (vừa) ─ fail ─▶ Debugger (mạnh) ─ fail ─▶ báo người
```

**Giảm chi phí khác:**
- **Prompt caching:** phần context chung (luật, spec, interface) đặt ở đầu prompt, dùng
  chung cho mọi agent trong một lần fan-out.
- **Context tối thiểu:** mỗi agent chỉ nhận phần spec và file của task mình, không nhận
  cả dự án.
- **Log rút gọn:** adapter trả JSON và đoạn log liên quan, không đổ log thô.
- **Tool trước, AI sau:** việc code làm được (tính địa chỉ, sinh package, check) thì
  không gọi AI.

**Chọn model bằng số liệu, không bằng cảm giác:** evals chạy theo từng vai và từng model
(mục 14.4). Vai nào model nhỏ đạt điểm gần model lớn thì hạ hạng. Model mới ra thì chạy
evals trước khi đổi.

```yaml
# trong .icagent.yml
models:
  tiers: { small: claude-haiku-4-5, medium: claude-sonnet-5, large: claude-opus-5-5 }
  roles:
    reporter: small
    coder: medium
    tester: medium
    debugger: { start: medium, escalate: large }
    planner: large
    reviewer: large
  nda_files: local            # file có nhãn nda chỉ đi qua model local (mục 14.3)
```

### 7.7 Gộp kết quả đúng khi fan-out

| # | Cơ chế | Chống lỗi gì |
|---|---|---|
| F1 | **Chốt interface trước, code song song sau.** Port, tham số, giao thức giữa các phần lấy từ spec máy đọc (mục 6.2) và đóng băng trước khi fan-out | Các phần ghép không khớp nhau |
| F2 | **Tập ghi không giao nhau.** Orchestrator kiểm trước khi phát: không hai task nào ghi chung file. File chung (package, top) chỉ do generator hoặc một task tích hợp duy nhất ghi, sau khi các nhánh xong | Ghi đè, xung đột |
| F3 | **Mỗi nhánh một workspace riêng** (git worktree); orchestrator merge. Xung đột merge là lỗi, không tự giải bằng AI | Nhánh này làm hỏng nhánh kia |
| F4 | **Kết quả có cấu trúc.** Mỗi agent trả JSON theo schema: file đã ghi, trạng thái, **giả định đã dùng**, câu hỏi mở. Sai schema thì coi như fail | Kết quả mơ hồ, không gộp được |
| F5 | **So giả định giữa các nhánh.** Hai nhánh hiểu spec khác nhau (ví dụ một bên coi interrupt là level, bên kia là pulse) thì dừng và hỏi người | Mỗi phần đúng riêng nhưng sai khi ghép |
| F6 | **Bước join luôn có kiểm tích hợp.** Sau khi gộp: lint và sim ở mức block (hoặc top), không chỉ test từng module | Lỗi chỉ lộ ra khi ghép |
| F7 | **Chọn bản bằng tool.** Với best-of-N: bản pass test trước, rồi ít cảnh báo, rồi area nhỏ. AI làm giám khảo chỉ khi hòa | AI chọn nhầm bản "trông đẹp" |
| F8 | **Lỗi một nhánh không kéo đổ cả lần fan-out.** Nhánh fail được thử lại riêng; nhánh xong được giữ | Tốn lại chi phí cho phần đã đúng |
| F9 | **Gộp theo thứ tự cố định** (theo id task), để chạy lại cho cùng kết quả | Kết quả khác nhau mỗi lần chạy |

```
            chốt interface (F1) ── kiểm tập ghi (F2)
                          │
        ┌─────────────────┼─────────────────┐
     worktree A        worktree B        worktree C      (F3)
     Coder+Tester      Coder+Tester      Coder+Tester
        │ JSON (F4)       │                 │
        └────────┬────────┴─────────────────┘
                 ▼
     so giả định (F5) ─ lệch ─▶ hỏi người
                 ▼
     merge theo thứ tự (F9) ─ xung đột ─▶ báo lỗi
                 ▼
     task tích hợp: generator/top (F2) ─▶ lint + sim mức block (F6)
                 ▼
             Reviewer ─▶ Gate
```

### 7.8 Lớp quyết định nhanh

Trong một run có rất nhiều **quyết định nhỏ, có đáp án dạng lựa chọn**, không cần sinh
chữ. Gọi model mạnh cho từng cái là lãng phí. Core có một hàm chung:

```
decide(câu hỏi, các lựa chọn hoặc schema, context) -> { giá trị, độ tin cậy }
```

| Quyết định | Lựa chọn |
|---|---|
| Lỗi này thuộc loại nào? | hạ tầng / RTL / testbench / spec |
| Giao lỗi cho ai? | Debugger RTL / Debugger TB / hỏi người |
| Có cần leo hạng model không? | giữ / leo / báo người |
| Run có đang tiến triển không? | tiến / kẹt / dao động |
| Lệnh tool này có rủi ro không? | cho chạy / cần duyệt / chặn |
| Câu spec này có mơ hồ không? (lọc trước khi gọi Spec Critic) | rõ / mơ hồ |
| Có cần hỏi người không? | có / không |

**Thứ tự backend:**
1. **Luật bằng code** khi luật viết được (ví dụ: API trả 429 thì là lỗi hạ tầng).
2. **Model quyết định nhanh** (một model phân loại chuyên dụng như Jev, hoặc model nhỏ
   với structured output).
3. **Model mạnh** khi độ tin cậy dưới ngưỡng.

Ngưỡng tin cậy được cấu hình cho từng loại quyết định. Mọi quyết định được ghi log để
làm evals và chỉnh ngưỡng. Đây là mô hình "System 1 / System 2": việc nhanh, có khuôn cho
model nhanh; việc khó, cần sinh nội dung cho model mạnh.

**Không dùng lớp này để** sinh RTL, viết spec hay review nội dung. Những việc đó cần model
mạnh.

---

## 8. Tools (MCP server)

| Tool | Dựa trên | Trả về |
|---|---|---|
| `spec_check` | JSON Schema + script chéo | danh sách lỗi có vị trí |
| `lint` | Verilator `-Wall`, Verible | lỗi/cảnh báo có file:line |
| `naming` | luật từ profile | vi phạm |
| `ports_diff` | slang/verible parse | khác biệt spec ↔ RTL |
| `sim` | Verilator, Icarus, (VCS nếu có) | pass/fail, log rút gọn |
| `wave` | đọc VCD/FST | tín hiệu quanh thời điểm lỗi |
| `formal` | SymbiYosys | pass/fail, counterexample |
| `synth` | Yosys, (DC nếu có) | area, cell, cảnh báo latch |
| `sta` | OpenSTA, (PrimeTime nếu có) | slack, đường tệ nhất |
| `gen` | generator của tool | package, header, bảng, top |

Mọi tool trả **kết quả có cấu trúc (JSON) + log rút gọn**, không đổ log thô cho agent.
Bảng trên là adapter ví dụ; agent chỉ thấy khả năng (`lint`, `sim`…), và profile chọn
adapter (mục 5.4). Mỗi dự án có thể trỏ thẳng vào `make` riêng của mình.

---

## 9. Profile dự án

Profile là tầng "dự án" trong cấu hình nhiều tầng (mục 5.7). Nó chỉ khai báo phần khác
với tổ chức và preset.

```yaml
# .icagent.yml ở gốc repo dự án
project: qsoc
extends: [org:qnsc, preset:mcu-soc]      # luật chung của công ty + bộ mẫu MCU SoC
packs: [spec-core, lang-sv, digital-rtl, dv, pm]   # chưa bật backend, firmware

spec:
  chip: { path: util/qsoc_contract.yml, format: qsoc-contract }   # format adapter
  ip_dir: doc/specs
  registers: { format: none }            # chưa dùng SystemRDL

adapters:                                # khả năng -> adapter
  lint:  { use: make, cmd: "make lint BLOCK={block}", parser: verilator }
  sim:   { use: make, cmd: "make sim BLOCK={block}" }
  synth: { use: make, cmd: "make syn BLOCK={block}", parser: dc }
  parse: { use: pyslang }

naming:
  rule_doc: doc/rules/QNSC_RTL_Design_Naming_Rule.pdf
  checker: "python3 flow/lint/naming_check.py {file}"
filelist: "design/{block}/{block}.f"
target: { kind: asic, pdk: gf180 }

workflow_overrides:
  rtl_block:
    add_stage: { after: implement, id: hardcode, run: "make hardcode BLOCK={block}" }

reviewers: [nghia]
```

Dự án mới chỉ viết file này (và, nếu cần, adapter hay luật riêng); lõi tool không đổi.

---

## 10. Knowledge base

| Kho | Nội dung |
|---|---|
| Thư viện IP | Mỗi IP một thẻ: nguồn, license, bus, port, interrupt, clock, lưu ý tích hợp |
| Dự án cũ | PRD, HAS, MAS, contract, bài học (QSoC là dự án đầu) |
| Template | PRD, HAS, MAS, `chip.yml`, `<ip>.yml`, kèm checklist bắt buộc |
| Luật | Naming rule, coding rule, quy trình review |

Dự án càng nhiều, kho càng giàu, AI soạn càng tốt. Đây là giá trị lâu dài của tool.

---

## 11. Lệnh cho người dùng

| Lệnh | Tầng | Việc |
|---|---|---|
| `/intake` | PRD | Phỏng vấn, viết PRD |
| `/arch` | HAS | Đề xuất kiến trúc, viết HAS + `chip.yml` |
| `/mas <ip>` | MAS | Soạn MAS + `<ip>.yml`/`.rdl` |
| `/spec-check` | mọi tầng | Chạy check + Spec Critic |
| `/plan <ip>` | RTL | Chia task |
| `/rtl <ip>` | RTL | Chạy vòng Coder/Tester/Checker/Debugger |
| `/wrap <ip>` | RTL | Sinh wrapper cho IP vendor |
| `/verify <ip>` | DV | TB, SVA, vòng coverage |
| `/review` | mọi tầng | Reviewer context mới |
| `/change <mô tả>` | mọi tầng | Mở yêu cầu thay đổi (mục 13) |
| `/impact <change>` | mọi tầng | Phân tích tác động một thay đổi |
| `/status` | mọi tầng | Cái gì mới, cái gì cũ (stale), task nào đang chờ người |
| `/resume` | mọi tầng | Tiếp từ checkpoint cuối (mục 14.1) |
| `/research <câu hỏi>` | mọi tầng | Researcher tìm và đề xuất có nguồn (mục 14.2) |
| `/report` | quản lý | Cập nhật tracker, báo cáo |
| `/eval` | tool | Chạy bộ bài thi |

---

## 12. Cách dùng

Tool không phải chỉ là một bộ skill, cũng không tự động hoàn toàn. Người dùng chọn
**chế độ** theo tình huống và **mức tự động** theo loại việc.

### 12.1 Ba chế độ

| Chế độ | Dùng khi | Cách chạy | Ví dụ |
|---|---|---|---|
| 1. Trợ lý (tương tác) | Viết spec, ra quyết định, học | Gõ lệnh trong Claude Code, trao đổi qua lại | `/mas timer`: AI hỏi, soạn nháp, người sửa ngay |
| 2. Tự chạy giữa các gate | Việc dài, rõ ràng: RTL, test, debug | Một lệnh; orchestrator chạy cả chuỗi task, chỉ dừng ở gate hoặc khi bí | `/rtl timer`: "xong 7/8 task, T05 cần người quyết" |
| 3. Bot trong CI | Kiểm mọi PR của team | GitHub Actions gọi CLI, không ai phải gõ lệnh | PR đổi contract: bot comment danh sách bị ảnh hưởng; PR RTL: bot review theo spec |

### 12.2 Mức tự động

Cấu hình trong profile, cho từng loại việc:

| Mức | AI làm | Người làm | Mặc định cho |
|---|---|---|---|
| 0. Gợi ý | Nhận xét, đặt câu hỏi | Tự viết | Quyết định kiến trúc |
| 1. Soạn nháp | Viết nháp, chờ duyệt từng bước | Duyệt từng bước | PRD, HAS, MAS |
| 2. Tự chạy tới gate | Làm cả chuỗi, dừng ở gate | Duyệt ở gate | RTL, TB, SVA, debug |
| 3. Tự merge | — | — | **Không dùng.** PR luôn có người duyệt |

```yaml
# trong .icagent.yml
autonomy:
  prd: 1
  has: 1
  mas: 1
  rtl: 2
  verify: 2
```

### 12.3 Các thành phần phối hợp thế nào

```
Người gõ /rtl timer                  ← COMMAND: cửa vào
        ▼
Orchestrator (CLI icagent)           ← ĐỘNG CƠ: máy trạng thái, tasks, gate
        ▼ giao task
Agent Coder / Tester / Debugger      ← THỢ: mỗi agent chỉ có quyền đúng việc
        ▼ nạp khi cần
Skill "naming rule", "viết wrapper"  ← KIẾN THỨC: cách làm một loại việc
        ▼ gọi
MCP tools: lint, sim, synth          ← TRỌNG TÀI
```

- **Cài đặt:** một lệnh cài plugin vào Claude Code (commands, agents, skills, hooks,
  cấu hình MCP) và CLI `icagent`.
- **Cùng một CLI** chạy ở ba chỗ: gọi từ plugin, chạy trong terminal
  (`icagent run rtl timer`), chạy trong CI.

### 12.4 Bắt đầu một dự án mới

Người đưa **yêu cầu ban đầu**; tool sinh ra PRD, HAS, contract, MAS **theo từng bước có
gate**, không một phát ra hết. Mỗi bước AI soạn và hỏi, người trả lời và chốt.

| Bước | Lệnh | Người đưa vào | Tool sinh ra | Gate |
|---|---|---|---|---|
| 0 | `icagent init` | Tên dự án, chọn profile mẫu | `.icagent.yml`, cây thư mục spec | — |
| 1 | `/intake` | Ý tưởng, ràng buộc (ứng dụng, interface, tốc độ, công suất, diện tích, process, ngân sách) | `spec/PRD.md` với REQ-ID cấp sản phẩm, danh sách câu hỏi | **G0**: chốt PRD |
| 2 | `/arch` | Chọn 1 trong 2–3 phương án kiến trúc | `spec/HAS.md` + `spec/chip.yml`, truy vết về REQ của PRD | **G1a**: chốt kiến trúc |
| 3 | `/mas <ip>` (song song, mỗi chủ IP một block) | Trả lời câu hỏi của Spec Critic | `<ip>_MAS.md` + `<ip>.yml`/`.rdl` | **G1b**: chủ IP chốt từng MAS |
| 4 | `/plan`, `/rtl`, `/verify` | Duyệt plan, trả lời khi tool hỏi | RTL, TB, SVA, PR | **G2**, **G3** |

Tool không quyết thay người ở bước 1–2: nó đưa phương án và hỏi. Cái nó tiết kiệm là
công soạn, tra cứu, giữ nhất quán, chứ không phải công suy nghĩ kiến trúc.

### 12.5 Ví dụ một ngày làm TIMER

1. **Sáng:** `/mas timer` (chế độ 1). AI hỏi 5 câu, người trả lời, AI soạn MAS và
   `timer.yml`. `/spec-check` sạch, người chốt G1b.
2. **Trưa:** `/rtl timer` (chế độ 2), người đi họp. Orchestrator chạy 8 task.
3. **Chiều:** thông báo "7 task pass; T05 không rõ khi ghi `LOAD` lúc counter đang chạy".
   Người trả lời; câu trả lời được ghi vào MAS trước (mục 13), rồi tool chạy tiếp và mở PR.
4. **Tối:** bot CI (chế độ 3) review PR, comment. Người đọc và merge.

### 12.6 Framework hay sản phẩm

**Sản phẩm, xây trên một lõi nhỏ có điểm mở rộng.** Không làm framework đa dụng để người
khác tự xây agent.

| | Framework đa dụng | Sản phẩm trên lõi nhỏ (**chọn**) |
|---|---|---|
| Người dùng nhận được | Viên gạch, phải tự lắp | Lệnh chạy được ngay: `/mas`, `/rtl`, `/verify` |
| Giá trị nằm ở | API tổng quát | Quy trình chip, luật, kiến thức, check |
| Chi phí giữ | API phải ổn định, tài liệu dày, hỗ trợ nhiều kiểu dùng | Chỉ giữ ổn định plugin API cho pack/adapter |
| Rủi ro | Xây lâu, chưa ai dùng | Thấp: mỗi bản dùng thật trên QSoC |

- **Tính tổng quát vẫn giữ** qua pack, adapter, preset, workflow dạng dữ liệu (mục 5).
  Đó là điểm mở rộng của sản phẩm, không phải framework để bán.
- **Không xây trên framework agent nặng** (LangChain, CrewAI…): dùng Claude Agent SDK và
  orchestrator tự viết, để không bị khóa và hiểu hết mọi dòng code.
- Khi có từ 2–3 dự án dùng thật và plugin API đã ổn, có thể tách lõi thành thư viện
  riêng. Không làm trước.

### 12.7 Giao diện theo dõi

**Có cần, nhưng làm theo bậc.** Nguyên tắc: **giao diện chỉ là cửa sổ nhìn vào state trong
git**, không có nguồn dữ liệu riêng. Duyệt gate trên web hay trong terminal đều ghi vào
cùng một journal.

| Bậc | Khi nào | Giao diện |
|---|---|---|
| **1. Không có UI riêng** | v0.1–v0.2, một người dùng | Terminal (`status`, `report`), `HANDOFF.md`, GitHub (PR, Actions), Langfuse cho trace |
| **2. Trang tĩnh sinh ra** | v0.3, team bắt đầu dùng | `icagent site`: sinh trang HTML từ state (giống tracker), đăng trên GitHub Pages hoặc nội bộ. Chỉ để xem |
| **3. Dashboard web** | v0.4+, nhiều người, nhiều dự án | Web app nhẹ, có thao tác: trả lời câu hỏi, duyệt gate, chạy lại task |
| **4. Tích hợp editor** | Khi cần | Extension VS Code: hiện REQ, trạng thái, câu hỏi ngay cạnh file RTL |

**Dashboard cần cho thấy:**

| Màn hình | Nội dung |
|---|---|
| Tổng quan dự án | Ma trận block × stage (spec, RTL, DV, …), màu theo trạng thái; giống `TRACKER.md` nhưng tự cập nhật |
| Hộp chờ người | Gate đang chờ, câu hỏi mở, ai phải trả lời, đã chờ bao lâu |
| Dòng thời gian một run | Từng bước, agent nào, tool nào, pass/fail, token, tiền; nhảy sang trace chi tiết |
| Truy vết | REQ → spec → RTL → test; bấm một REQ thấy mọi thứ liên quan |
| Tác động thay đổi | Bảng `/impact`: tự sinh lại, cần làm lại, không ảnh hưởng |
| Chất lượng và chi phí | Các chỉ số mục 14.4 theo thời gian; điểm evals theo model và vai |

**Không làm:** trình soạn RTL hay spec trên web (đã có editor và git), hệ quản lý
task thay GitHub (chỉ đọc và liên kết sang).

---

## 13. Quản lý thay đổi

Requirement và spec **sẽ đổi** giữa chừng, kể cả khi đã có RTL. Tool xử lý như một
**build system hiểu spec**: biết cái gì sinh ra từ cái gì, nên biết chính xác cái gì bị cũ.

### 13.1 Nguyên tắc

| # | Nguyên tắc |
|---|---|
| C1 | **Sửa ở nguồn, chảy xuống dưới.** Đổi ở tầng nào thì sửa tài liệu tầng đó trước; tầng dưới được cập nhật theo, không vá thẳng |
| C2 | **Không sửa RTL khi spec chưa đổi.** Lỗi phát hiện khi code/verify mà do spec thì mở yêu cầu thay đổi đi **ngược lên**, sửa spec trước, rồi chảy xuống lại |
| C3 | **Mọi thay đổi có duyệt.** Giống change request (ECO): người duyệt thay đổi và bảng tác động trước khi tool làm |
| C4 | **Chạy lại toàn bộ test**, không chỉ phần đổi, để bắt tác dụng phụ |
| C5 | **Có phiên bản.** Mọi tài liệu có version và changelog; mọi thứ nằm trong git |

### 13.2 Đồ thị truy vết

```
PRD REQ-P03 ─▶ HAS §4.2 ─▶ chip.yml: ip.gpio_3 ─▶ gpio_MAS REQ-011 ─▶ rtl/gpio.sv ─▶ tb_gpio
                              │
                              └─▶ (sinh ra) qnsc_pkg.sv · header C · bảng MAS · top
```

Mỗi thứ được sinh ra hoặc được AI viết đều ghi lại **dấu vân tay (hash) của các input**
nó dựa vào, trong `.icagent/trace.yml`. Input đổi thì hash lệch, nên thứ đó bị đánh dấu
**stale** (cũ), giống cách `make` biết file nào cần build lại.

### 13.3 Luồng một thay đổi

```
1. Người: /change "bỏ GPIO3"                    (hoặc agent phát hiện lỗi spec, đi ngược lên)
2. Tool xác định tầng nguồn (ở đây: HAS + chip.yml), soạn bản sửa ở tầng đó
3. /impact đi theo đồ thị xuống dưới, chia 3 nhóm:
     tự sinh lại   : địa chỉ các APB slave sau, qnsc_pkg.sv, header C, bảng MAS
     cần làm lại   : GPIO MAS, nối dây top, IO MUX, test dùng địa chỉ cũ   (stale)
     không ảnh hưởng: TIMER, PWM, INTMAP logic, ...
4. [Gate C: người duyệt bản sửa + bảng tác động]
5. Orchestrator sinh lại nhóm 1, tạo task chỉ cho nhóm 2, chạy lại toàn bộ test
6. Một branch, một PR; bump version tài liệu, ghi changelog
```

### 13.4 Đóng băng theo giai đoạn

Càng về cuối dự án, thay đổi càng đắt. Profile khai báo mức đóng băng cho từng tầng:

```yaml
freeze:
  prd: after_G1a        # sau khi chốt kiến trúc, đổi PRD cần người lead duyệt
  chip: after_rtl_freeze
  mas: after_rtl_freeze
approvers:
  frozen: [nghia, teacher]
```

Thay đổi vào tầng đã đóng băng vẫn làm được, nhưng cần người duyệt cấp cao hơn, và
`/impact` hiện rõ chi phí (số task, số test phải chạy lại).

---

## 14. Độ tin cậy và vận hành

Tool chỉ có ích khi **chạy ổn định, dừng đâu tiếp đó được, biết rõ mình đang ở đâu, và
chứng minh được là nó làm đúng**. Mục này gom các cơ chế cho việc đó.

### 14.1 Resume: dừng ở đâu, tiếp từ đó

| Cơ chế | Cách làm |
|---|---|
| **Nhật ký sự kiện** | Mọi bước ghi vào `.icagent/journal.jsonl` (chỉ ghi thêm, không sửa): bắt đầu task, gọi agent, gọi tool, kết quả, gate, câu trả lời của người. State hiện tại được dựng lại từ nhật ký |
| **State cho máy dạng JSON/YAML, cho người dạng Markdown** | Danh sách task và trạng thái để ở JSON/YAML (model ít sửa bậy hơn Markdown, theo kinh nghiệm của Anthropic); `HANDOFF.md` chỉ để người đọc |
| **Khóa idempotency cho tool call** | Mỗi lần gọi tool có khóa; resume không chạy lại lần gọi đã xong (tránh sim hay synth tốn giờ chạy hai lần) |
| **Thủ tục đầu phiên** | Mỗi phiên agent bắt đầu cố định: đọc state, `HANDOFF.md`, git log; chạy `doctor` và một smoke check; rồi mới làm task tiếp |
| **Test resume** | Bộ test cố ý ngắt ở từng loại bước (giữa agent, giữa tool, ở gate) rồi `resume`, kiểm kết quả giống chạy liền một mạch |
| **Checkpoint sau mỗi bước** | Xong một bước thì ghi state và commit vào branch làm việc. Máy tắt, mất mạng, API lỗi: không mất gì ngoài bước đang chạy |
| **Bước idempotent** | Chạy lại một bước cho cùng kết quả hoặc bỏ qua nếu đã xong (so hash input, giống mục 13.2) |
| **Một run một branch** | `icagent/<workflow>/<block>/<run-id>`; state nằm trong git nên tiếp tục được trên máy khác, hoặc người khác tiếp |
| **Khóa** | Một block chỉ có một run ghi tại một thời điểm, tránh hai người (hoặc hai agent) ghi đè nhau |
| **Lệnh** | `icagent status`: đang ở stage nào, task nào xong, task nào chờ người, tốn bao nhiêu. `icagent resume`: tiếp từ checkpoint cuối. `icagent rewind <step>`: quay lại một bước |

**Biết đang ở đâu:** mỗi lần dừng (gate, lỗi, hết ngân sách), tool viết một **ghi chú bàn
giao** ngắn trong `.icagent/HANDOFF.md`:
- đã làm gì;
- đang chờ gì, chờ ai;
- câu hỏi mở;
- bước tiếp theo.

Người đọc là hiểu ngay. Agent mới cũng đọc file này làm context, thay vì phải nhớ cả
cuộc hội thoại cũ.

### 14.2 Research để đề xuất

**Có**, nhưng là một agent có kiểm soát, không phải "AI tự lên mạng rồi làm theo":

| Mặt | Quy định |
|---|---|
| Agent | **Researcher**: tìm, so sánh, tóm tắt, **luôn kèm nguồn** |
| Thứ tự tìm | 1) Knowledge base nội bộ, dự án cũ; 2) tài liệu IP, datasheet, chuẩn (AMBA, RISC-V…); 3) paper, repo open-source; 4) web chung |
| Dùng khi | `/arch` (so phương án, chọn IP); chọn IP vendor (license, độ chín); debug (errata, lỗi đã biết); đề xuất cải tiến |
| Kết quả | **Đề xuất có nguồn**, không phải quyết định. Người chọn ở gate |
| Học lại | Kết quả được người duyệt thì lưu vào knowledge base (thẻ IP, bài học), để lần sau khỏi tìm lại |
| Bảo mật | Không đưa nội dung NDA, tên dự án nội bộ hay số liệu mật vào câu truy vấn web; nguồn được phép khai báo trong cấu hình tổ chức |

Ngoài research theo yêu cầu, tool có thể chạy **định kỳ** (ví dụ hàng tuần): kiểm IP
vendor có bản mới hay lỗi bảo mật, tool EDA có bản mới, paper mới trong lĩnh vực. Kết quả
là một báo cáo đề xuất, không tự áp dụng.

### 14.3 Các cơ chế đảm bảo khác

| Nhóm | Cơ chế |
|---|---|
| **Quan sát được** | Mỗi lần gọi agent/tool đều ghi: prompt, tool đã gọi, kết quả, token, chi phí, thời gian, theo chuẩn OpenTelemetry GenAI (xem được bằng Langfuse hay backend khác, không khóa nhà cung cấp). `icagent report <run>` cho báo cáo một run; tổng hợp theo tuần |
| **Tái lập được** | Mỗi run ghi một manifest: version model, version prompt/pack/adapter, version tool EDA, hash input. Chạy lại cùng manifest thì so được kết quả |
| **Giới hạn và sandbox** | Agent chỉ ghi đúng file của task; không `git push`, không xóa ngoài phạm vi; mạng chỉ mở cho Researcher; chặn lệnh nguy hiểm; trần token, tiền và thời gian cho mỗi run |
| **Phát hiện kẹt** | Cùng một lỗi lặp lại 2 lần, hoặc diff dao động qua lại, thì dừng và báo người, không đốt tiếp ngân sách |
| **Lỗi hạ tầng** | API lỗi: retry có backoff. Tool EDA treo: timeout. License EDA hết: chờ và báo. Không bao giờ coi lỗi hạ tầng là lỗi thiết kế |
| **Không chắc thì hỏi** | Agent phải đánh dấu chỗ nó không chắc (spec mơ hồ, hai cách hiểu). Chỗ quan trọng có thể cho 2–3 lần sinh độc lập rồi so; lệch nhau thì hỏi người |
| **Sức khỏe môi trường** | `icagent doctor`: kiểm tool EDA, version, license, API key, quyền ghi, profile hợp lệ trước khi chạy |
| **Thông báo** | Gate đang chờ, run lỗi, run xong: báo qua terminal, và tùy chọn Slack hoặc email |
| **Học từ người duyệt** | Mỗi lần người sửa hoặc từ chối ở gate, tool ghi lại lý do. Định kỳ gom thành đề xuất sửa skill/luật, người duyệt rồi mới áp dụng |
| **Kiểm chính tool** | Unit test cho core; bộ test chung cho mỗi adapter; test đầu-cuối trên một dự án mẫu nhỏ; evals chạy trong CI của tool mỗi khi đổi prompt, pack hoặc model |
| **Đổi model an toàn** | Model mới chạy evals trước (canary). Chỉ chuyển khi điểm không tụt |
| **Nâng cấp state** | Schema của state và journal có version; tool có bước migrate khi nâng cấp |
| **Dữ liệu** | Mỗi file có nhãn `public` / `internal` / `nda`. LLM adapter đọc nhãn để chặn hoặc chuyển sang model local |

### 14.4 Thước đo "tool work well"

| Chỉ số | Ý nghĩa |
|---|---|
| Tỉ lệ task pass không cần người sửa | Chất lượng agent |
| Số lượt Debugger trung bình mỗi task | Độ khó và hiệu quả vòng sửa |
| Tỉ lệ bị từ chối ở gate, và lý do | Chỗ agent còn yếu |
| Lỗi lọt qua gate, phát hiện muộn | Độ chặt của check và review |
| Token, tiền, thời gian mỗi block | Chi phí |
| Số lần resume thành công sau khi dừng | Độ bền |
| Thời gian người bỏ ra mỗi block, so với làm tay | Giá trị thật cho team |

Các chỉ số này xem được bằng `icagent report`, và là căn cứ quyết định đổi prompt, model
hay quy trình.

---

## 15. Phủ các giai đoạn của WBS

| Giai đoạn | Mức | Việc tool làm |
|---|---|---|
| Spec, kiến trúc | Mạnh | PRD, HAS, MAS, contract, kiểm khớp |
| Digital design | Mạnh | RTL, wrapper, lint, naming |
| Verification | Mạnh (v0.2) | TB, SVA, coverage, debug |
| Backend | Trung bình (v0.3) | Đọc report synth/STA/PnR, gợi ý SDC |
| Software | Trung bình | Header, driver từ register map |
| Quản lý | Trung bình | Tracker, tóm tắt PR, báo cáo tuần |
| Analog, board | Sau | MCP cho ngspice, KiCad khi cần |

---

## 16. Công nghệ

| Phần | Chọn | Phương án thay thế |
|---|---|---|
| Orchestrator | Python, máy trạng thái tự viết, state YAML/SQLite | LangGraph (có checkpointer); Temporal nếu cần chạy dài trên server |
| Gọi agent | Claude Agent SDK (subagent, giới hạn tool theo agent) | Claude Code plugin thuần; LiteLLM nếu cần nhiều model |
| Tool | MCP server (Python) | Gọi CLI trực tiếp |
| Giao diện | Claude Code plugin + CLI; trang tĩnh rồi dashboard web (mục 12.7) | Extension VS Code |
| Spec máy đọc | YAML + JSON Schema; SystemRDL cho thanh ghi | IP-XACT; hjson kiểu OpenTitan |
| Parse RTL | slang (pyslang), Verible | Verilator XML |
| Quyết định nhanh (mục 7.8) | Luật + model nhỏ có structured output | Jev (TypeSafe AI), sau khi qua evals và chính sách dữ liệu |
| Quan sát (tracing) | Chuẩn OpenTelemetry GenAI; xem bằng Langfuse (open-source, tự host được) | Arize Phoenix |
| Evals | Inspect AI (UK AISI, MIT) | DeepEval; script tự viết |

---

## 17. Cấu trúc repo

```
qnsc-icagent/
  DESIGN.md
  core/             KHÔNG biết gì về chip
    orchestrator/     máy trạng thái, DAG, gate, budget
    state/            tasks, trace graph, hash
    runtime/          chạy agent, luật quyền đọc/ghi
    plugin_api/       giao diện cho pack và adapter
    llm/              giao diện LLM adapter
  packs/            kiến thức theo lĩnh vực (mỗi pack có pack.yml)
    spec-core/        schema lõi, check chéo, generator, agent spec
    lang-sv/          luật, skill, parser cho SystemVerilog
    digital-rtl/      coder, debugger, workflow rtl_block
    dv/               tester, SVA, coverage
    pm/               tracker, báo cáo
  adapters/
    tool/             verilator, verible, iverilog, yosys, make, ...
    format/           qsoc-contract, systemrdl, ip-xact, opentitan-hjson
    llm/              claude, ...
  interfaces/       thư viện bus dạng dữ liệu (apb, axi4, ahb, wishbone, ...)
  presets/          mcu-soc, ip-block, fpga-prototype, ...
  orgs/             cấu hình mẫu cấp tổ chức (qnsc)
  mcp/              MCP server bọc các khả năng cho Claude Code và client khác
  plugin/           Claude Code commands, hooks
  evals/            bài thi theo nhiều dự án + script chấm
  docs/
```

Repo dự án (ví dụ QSoC) chỉ thêm `.icagent.yml` và thư mục state `.icagent/`.

---

## 18. Lộ trình

| Bản | Nội dung | Tiêu chí xong |
|---|---|---|
| v0.1 | Core tối thiểu (orchestrator, state, plugin API, journal, checkpoint, `status`/`resume`, `doctor`, log chi phí). Pack `digital-rtl` + `lang-sv` nhỏ. Adapter `make`, `verilator`. Agent: Coder, Debugger. Profile QSoC | Sinh một module QSoC qua CI không sửa tay; tắt giữa chừng rồi `resume` đúng chỗ; CI chặn core import pack/adapter |
| v0.2 | Spec model: schema, check chéo, generator. Đồ thị truy vết, `/change`, `/impact`. Fan-out theo task với worktree, tập ghi không giao nhau, bước join. Chọn model theo vai + cascade. Agent: Spec Writer, Spec Critic, Planner. Gate 1–2 | Viết lại spec TIMER/SYSDBG bằng tool, check chéo sạch; diễn lại thay đổi "bỏ GPIO3" và ra đúng bảng tác động |
| v0.3 | Verification: Tester, SVA, vòng coverage, Reviewer. Evals đầu tiên (INTMAP, PWM). Ghi lý do người sửa ở gate; `icagent report` với các chỉ số mục 14.4. Trang trạng thái tĩnh `icagent site` | Có số % pass đo được |
| v0.4 | `/intake`, `/arch`, knowledge base, agent Researcher; backend report. Dashboard web: hộp chờ người, duyệt gate | Chạy từ PRD đến HAS cho một chip mẫu |
| v0.5 | Preset `ip-block` + adapter open-source (iverilog, yosys) chạy trên một IP lẻ Sky130; format adapter OpenTitan hjson | Dự án loại thứ hai chạy được, không sửa core |
| v1.0 | Áp dụng cho **dự án chip thật thứ hai** của team | Chỉ viết profile (và adapter nếu cần); core và pack không đổi |

---

## 19. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Spec sai làm RTL và test cùng sai | Spec Critic, Cross-IP Reviewer, Gate 1, người duyệt |
| Agent ảo giác số liệu | Số liệu chỉ từ YAML/RTL, code đối chiếu |
| Chi phí token cao | Ngân sách từng task, chọn model theo vai + cascade (mục 7.6), log rút gọn, prompt caching |
| Fan-out ghép sai | Chốt interface trước, tập ghi không giao nhau, so giả định, kiểm tích hợp (mục 7.7) |
| **Lộ dữ liệu IP/PDK bảo mật khi gửi lên cloud** | Chính sách dữ liệu theo profile; không gửi tài liệu dưới NDA; cân nhắc model chạy local cho phần nhạy cảm |
| Phụ thuộc một nhà cung cấp model | Lõi và MCP độc lập model; lớp gọi agent thay được |
| Tool quá phức tạp so với team | Làm theo bản nhỏ, mỗi bản dùng thật trên QSoC |
| Trừu tượng hóa quá sớm, xây framework không ai dùng | Điểm mở rộng có từ đầu, nhưng adapter/preset thứ hai chỉ viết khi có dự án thật (mục 5.8) |
| Vô tình gắn cứng QSoC vào core | Luật phụ thuộc kiểm bằng CI; chạy trên ít nhất 3 loại dự án |

---

## 20. Câu hỏi để thảo luận

1. Tên chính thức của tool?
2. Repo private hay public trong org? License?
3. Chỉ Claude hay cần đổi được model ngay từ đầu?
4. Định dạng spec máy đọc: YAML tự định nghĩa, SystemRDL, IP-XACT, hay kết hợp?
5. Orchestrator tự viết hay dùng LangGraph?
6. Chạy ở đâu: máy cá nhân, CI, server chung của team?
7. Chính sách dữ liệu với PDK, IP vendor dưới NDA?
8. Ngân sách token/tháng cho team?
9. v0.1 áp dụng thử trên block nào của QSoC (TIMER, SYSDBG)?
10. Ai trong team cùng build, ai chỉ dùng?
11. Mức tự động mặc định cho từng loại việc (mục 12.2) có hợp không?
12. Mốc đóng băng và người duyệt thay đổi sau đóng băng (mục 13.4)?
13. Loại dự án nào cần hỗ trợ sau QSoC (IP lẻ, accelerator, FPGA, mixed-signal)?
14. Ngôn ngữ thiết kế nào ngoài SystemVerilog cần sớm (VHDL, Chisel)?
15. Tool thương mại nào team sẽ có (VCS, DC, PrimeTime…) để ưu tiên viết adapter?
16. Nguồn nào Researcher được phép dùng; có chạy research định kỳ không?
17. Kênh thông báo khi gate chờ người (terminal, Slack, email)?
18. Trần chi phí mỗi run và mỗi tháng?
19. Số agent tối đa chạy song song (theo rate limit API và license EDA)?
20. Bảng hạng model cho từng vai (mục 7.6) có hợp không?
21. Có thử Jev cho lớp quyết định nhanh (mục 7.8) không, khi nào (sau evals v0.3)?
22. Dashboard đặt ở đâu (GitHub Pages, server nội bộ), ai được xem và duyệt?

---

## 21. Tham khảo

- MAGE — https://arxiv.org/pdf/2412.07822
- Spec2Cov — https://arxiv.org/pdf/2604.15606 · LLM4Cov — https://arxiv.org/pdf/2602.16953 · AgentDV — https://arxiv.org/pdf/2608.27148
- CVDP benchmark — https://arxiv.org/abs/2506.14074
- LLMs in Digital EDA: from Generation to Orchestration — https://arxiv.org/abs/2608.27184
- Requirement ambiguity — https://arxiv.org/html/2604.21505v1
- Hierarchical IRs multi-agent — https://arxiv.org/abs/2608.30659
- Requirement formalization — https://arxiv.org/pdf/2604.18228
- MAST: Why do multi-agent LLM systems fail — https://arxiv.org/abs/2503.13657
- Anthropic: Building effective agents — https://www.anthropic.com/engineering/building-effective-agents
- Anthropic: Multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
- MetaGPT — https://arxiv.org/abs/2308.00352 · LangGraph — https://langchain-ai.github.io/langgraph/
- Gateflow Plugin — https://github.com/codejunkie99/Gateflow-Plugin
- VeriFlow-CC — https://github.com/bjwanneng/veriflow-cc
- MCP4EDA — https://github.com/NellyW8/MCP4EDA · ChipAgent — https://github.com/OpenGPGPU/chipagent
- VeriChat — https://arxiv.org/abs/2607.01668v1
- OpenTitan topgen — https://opentitan.org/earlgrey_1.0.0/book/util/topgen/index.html · reggen — https://opentitan.org/book/util/reggen/index.html · Comportability — https://opentitan.org/book/doc/contributing/hw/comportability/
- Anthropic: Effective harnesses for long-running agents — https://anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Durable execution, LangGraph và Temporal — https://appscale.blog/en/blog/durable-execution-llm-agents-temporal-langgraph-checkpointing-2026 · https://temporal.io/blog/manetu-the-thread-is-the-workflow
- Resume Means Resume (hợp đồng checkpoint/resume) — https://arxiv.org/pdf/2608.03836
- OpenTelemetry GenAI semantic conventions — https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions · Langfuse OTel — https://langfuse.com/integrations/native/opentelemetry
- Inspect AI và các framework evals — https://futureagi.com/blog/best-open-source-eval-frameworks-2026/
- Jev (TypeSafe AI) — https://typesafe.ai/blog/introducing-system-one-models-and-jev · REFLEX — https://arxiv.org/abs/2609.26532
- awesome-harness-engineering — https://github.com/ai-boost/awesome-harness-engineering
- Cadence ChipStack AI Super Agent — https://www.cadence.com/en_US/home/company/newsroom/press-releases/pr/2026/cadence-unleashes-chipstack-ai-super-agent-pioneering-a-new.html · Mental Model — https://www.hpcwire.com/2026/02/12/cadence-introduces-agentic-ai-system-for-chip-design-and-verification/
- Siemens Fuse EDA AI Agent — https://news.siemens.com/en-us/siemens-fuse-eda-ai-agent/ · self-verifying — https://news.siemens.com/en-us/siemens-nvidia-dac-2026/ · Questa One Agentic Toolkit — https://news.siemens.com/en-us/questa-one-agentic-ai-toolkit/
- Synopsys AgentEngineer — https://news.synopsys.com/2026-07-27-Synopsys-Advances-Agentic-AI-Chip-Design-with-AMD-and-Microsoft · Synopsys.ai Copilot — https://www.synopsys.com/blogs/chip-design/synopsys-ai-copilots-chip-design.html
- NVIDIA ChipNeMo — https://arxiv.org/abs/2311.00176 · Marco — https://arxiv.org/pdf/2504.01962 · OpenShell — https://blogs.nvidia.com/blog/secure-autonomous-ai-agents-openshell/
- Google AlphaChip — https://deepmind.google/blog/how-alphachip-transformed-computer-chip-design/ · circuit_training — https://github.com/google-research/circuit_training
- ChipAgents — https://www.businesswire.com/news/home/20260729819576/en/ChipAgents-Expands-Series-A-Funding-to-$134-Million-as-Demand-Grows-for-Agentic-AI-in-Semiconductor-Design · Bronco — https://bronco.ai/ · startup landscape — https://www.forbes.com/sites/karlfreund/2026/08/03/could-eda-ai-startups-be-the-new-claude-of-chip-design/
- EDA_MCP (SSH tới cụm EDA) — https://github.com/SiliCAD/EDA_MCP
- Synopsys/Cadence/Siemens agentic (Futurum) — https://futurumgroup.com/insights/synopsys-cadence-and-siemens-take-agentic-chip-design-autonomous-at-dac/
