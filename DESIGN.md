# chipgraph — Design

| Mục | Giá trị |
|---|---|
| Tên | **`chipgraph`** (chốt 2026-09-27, D22); CLI `chipgraph`, profile `.chipgraph.yml`, state `.chipgraph/` |
| Phát hành | **Public**, open-source (license: D23) |
| Chủ sở hữu, lead | Trong Nghia (Nghia VT) |
| Phiên bản | **v0.2**, thiết kế đích, **đã chốt** (2026-09-27, mọi quyết định trong DECISIONS); chưa có code |
| Ngày | 2026-09-27 |
| Dự án áp dụng đầu tiên | QSoC (`vlsi_deep_training`) |
| Tài liệu đi kèm | [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) (kế hoạch làm, cho agent) · [`AGENTS.md`](AGENTS.md) (luật cho agent làm repo này) · [`docs/DECISIONS.md`](docs/DECISIONS.md) (lý do từng lựa chọn) · [`docs/RESEARCH.md`](docs/RESEARCH.md) (research, tham khảo) · [`docs/archive/DESIGN_v0.1.md`](docs/archive/DESIGN_v0.1.md) (bản nháp thảo luận) |

v0.2 thay v0.1: gom các ý đã thảo luận về **một kiến trúc thống nhất**. Thay đổi chính
nằm ở mục 3 (build graph làm lõi) và mục 4 (Design Model).

---

## 0. Tóm tắt

**`chipgraph` là một phần mềm: hệ thống agent AI cho thiết kế chip.**

Người kỹ sư ra lệnh bằng ngôn ngữ tự nhiên. Tool làm ba việc:
- chia việc cho các agent AI;
- cho tool EDA kiểm từng bước;
- dừng lại để người duyệt ở các mốc quan trọng.

**Ý tưởng kiến trúc cốt lõi:** thiết kế chip là một **build graph**. Spec, RTL, test và
report đều là **artifact**. Mỗi artifact được tạo bởi một **rule**; một rule có thể là
code, là một agent AI, hoặc là người. Tool là một "`make` hiểu chip". Nó dựa trên một
**Design Model**, tức mô hình tri thức chung của thiết kế, và agent AI chỉ là **một loại
rule, loại không tất định**, nên luôn phải qua check và gate.

Từ một ý tưởng này suy ra được gần hết các tính năng đã bàn:

| Tính năng | Suy ra từ build graph thế nào |
|---|---|
| Resume | Build dừng ở đâu thì tiếp từ đó; artifact nào xong (hash khớp) thì bỏ qua |
| Quản lý thay đổi | Input đổi thì hash lệch, artifact phía sau thành **stale**; `/impact` là đi theo graph |
| Fan-out | Các target độc lập được build song song |
| Truy vết | Các cạnh của graph chính là REQ → spec → RTL → test |
| Gate | Rule loại "người duyệt" |
| Người và AI cùng sửa | Người sửa tay một file thì hash đổi, graph tự biết; không cần tách "chế độ tay" và "chế độ AI" |

Người dùng nhận được: CLI `chipgraph`, một plugin Claude Code (commands, skills, MCP), và
sau này một dashboard web. Mỗi dự án chip chỉ cần một file `.chipgraph.yml`.

---

## 1. Mục tiêu và phạm vi

Một AI tool **độc lập**, dùng cho **mọi dự án chip/IC** của team. Tool đi cùng kỹ sư từ
yêu cầu sản phẩm đến RTL đã kiểm chứng, rồi mở rộng sang verification, backend, firmware
và quản lý.

| Có | Không |
|---|---|
| Soạn và kiểm spec (PRD, HAS, MAS, contract) | Train model riêng |
| Sinh RTL, wrapper, testbench, SVA, script, có vòng kiểm bằng tool | Thay thế tool EDA |
| Điều phối agent theo build graph có gate | Agent tự trị không có người duyệt |
| Hỏi đáp, triage lỗi, phân tích tác động | Trình soạn thảo RTL trên web |
| Nhiều dự án qua pack, adapter, profile | Gắn cứng một chip, PDK, naming rule |

**Độc lập với flow của thầy:** chỉ học ý tưởng (gate, RTM). Không dùng lại file, prompt
hay code nào từ `VLSIT_RTL_Generator_AI_Model`.

---

## 2. Nguyên tắc

| # | Nguyên tắc |
|---|---|
| P1 | **AI soạn, code kiểm, người duyệt**, ở mọi tầng |
| P2 | **Tool là trọng tài, kiểm từng bước.** "Xong" nghĩa là check deterministic pass; mỗi bước sinh file đều qua check trước khi đi tiếp |
| P3 | **Engine điều phối, agent làm việc nhỏ.** Engine (code) quyết định bước tiếp theo; agent không điều khiển agent |
| P4 | **Mọi thứ là artifact có hash, mọi việc là rule có hợp đồng** (input, output, check, ngân sách) |
| P5 | **Một nguồn sự thật: Design Model.** Agent tra model, không tự suy từ file thô; số liệu sinh ra, không viết tay |
| P6 | **VCS của dự án là sự thật** (mặc định git; Perforce, SVN… qua adapter), còn cache và index sinh lại được |
| P7 | **Người viết và người kiểm độc lập**: test và code sinh riêng từ spec; reviewer dùng context mới |
| P8 | **Lõi không biết gì về chip cụ thể**: loại dự án, ngôn ngữ, bus, tool, PDK, luật đều là pack, adapter hoặc dữ liệu |
| P9 | **Dùng lại cái có sẵn**, cả trong dự án (format, `make`) lẫn trong cộng đồng (Edalize, PeakRDL, cocotb, pyslang) |
| P10 | **Không chắc thì hỏi, hết ngân sách thì dừng** |
| P11 | **Lõi bền, phần AI thay được.** Model, prompt, SDK, chiến lược điều phối là dữ liệu hoặc adapter; mọi giàn giáo quanh model gỡ được khi evals cho thấy hết cần (mục 16) |
| P12 | **Dấu vết tối thiểu trong repo của dự án.** Tool chỉ ghi vào repo dự án những gì team muốn giữ cùng thiết kế và đã duyệt. State chạy máy không vào repo. Mọi thứ khác là tùy chọn (mục 6.1) |

---

## 3. Kiến trúc

### 3.1 Bốn khái niệm của lõi

| Khái niệm | Là gì | Ví dụ |
|---|---|---|
| **Artifact** | Một thứ có nội dung, có kiểu, có hash, có nhãn dữ liệu (`public`/`internal`/`nda`) | `timer_MAS.md`, `timer.yml`, `m_qnsc_timer_cnt.sv`, `tb_timer_cnt.py`, report lint |
| **Rule** | Cách tạo ra artifact từ input. Có 4 loại: `gen` (code), `agent` (AI), `human` (người viết), `import` (từ format ngoài) | `gen:regblock` (PeakRDL), `agent:rtl_module`, `import:qsoc-contract` |
| **Check** | Kiểm tra deterministic trên artifact, qua tool adapter | `lint`, `naming`, `ports_diff`, `sim`, `spec_schema` |
| **Gate** | Người duyệt một nhóm artifact tại một hash cụ thể | `spec:timer`, `plan:timer`, `pr` |

Thêm hai thành phần:
- **Design Model**: kho tri thức có kiểu, dựng từ các artifact (mục 4).
- **Build graph**: các rule nối với nhau qua artifact.

### 3.2 Sơ đồ

```
 Người dùng
 ┌──────────────┬──────────────┬──────────────┬──────────────┐
 │ Claude Code  │ Terminal CLI │ CI (Actions) │ Dashboard    │
 │ plugin       │ chipgraph …  │ chipgraph …  │ (sau)        │
 └──────┬───────┴──────┬───────┴──────┬───────┴──────┬───────┘
        └──────── MCP / CLI API ──────┴──────────────┘
                          ▼
 ┌───────────────────────────────────────────────────────────┐
 │ ENGINE (core, không biết gì về chip)                      │
 │  scheduler build graph · stale/hash · gate · ngân sách    │
 │  journal · resume · fan-out · khóa · quyết định nhanh     │
 └───┬───────────────┬───────────────┬───────────────┬───────┘
     ▼               ▼               ▼               ▼
 DESIGN MODEL    AGENT RUNTIME   CHECK/TOOL       STATE (mục 6.1)
 (mục 4)         (mục 5)         ADAPTERS (mục 7) repo: artifact,
 typed facts,    Claude Code     Edalize, make,   decisions/
 query API       (mặc định),     pyslang, PeakRDL backend: journal,
                 Agent SDK,      local/ssh/lsf    run, cache, trace
                 generic; roles, skills
                 sandbox
     ▲               ▲               ▲
     └───────── PACKS: rule, role, skill, schema, check, template ──┘
                PROFILE: .chipgraph.yml (tool → org → preset → dự án → đường dẫn → block)
```

### 3.3 Một lệnh chạy thế nào

`/rtl timer` = build target `rtl:timer`:

```
1. Engine đọc profile, dựng graph cho target rtl:timer
2. Tính artifact nào stale (so hash)       ← resume và change đều nằm ở đây
3. Kiểm điều kiện: gate spec:timer đã duyệt ở đúng hash chưa? Chưa thì dừng, hỏi
4. Chạy rule theo thứ tự graph; nhánh độc lập chạy song song (worktree riêng)
     rule agent:rtl_module  ─┐ độc lập với nhau
     rule agent:tb_module   ─┘
     mỗi bước ghi file → check ngay (P2) → fail thì agent sửa trong ngân sách
5. Join: gen:package, gen:regblock → check tích hợp (lint + sim mức block)
6. Rule agent:review (context mới) → gate pr: **engine** (không phải agent) push branch và mở PR
7. Mỗi bước ghi journal; dừng bất kỳ lúc nào, `resume` tiếp đúng chỗ
```

### 3.4 Rule là dữ liệu

```yaml
# src/chipgraph/packs/digital_rtl/rules/rtl_module.yml
rule: rtl_module
kind: agent
role: author
skills: [lang-sv/rtl, org/naming]
foreach: model.plan(block).modules
inputs:
  - model: block/{block}            # port, clock, reset, param từ Design Model
  - spec:  "{block}#req:{module.reqs}"
outputs: ["design/{block}/rtl/{module.name}.sv"]   # tập ghi, không giao nhau
checks:  [lint, naming, ports_diff]
verified_by: tb_module              # rule độc lập, không thấy RTL này khi viết
budget:  { tries: 3, tier: medium, escalate: large }
```

Target và workflow là tập hợp rule. Pack cung cấp rule; profile bật, tắt hoặc ghi đè.

---

## 4. Design Model

### 4.1 Là gì

Mô hình tri thức **có kiểu** của toàn bộ thiết kế. Nó là nguồn sự thật mà mọi agent phải tra
(học từ "Mental Model" của Cadence ChipStack). Model được dựng từ ba nguồn:

| Nguồn | Cách lấy | Ví dụ fact |
|---|---|---|
| Spec máy đọc | Đọc trực tiếp | `chip.yml`: IP, địa chỉ, interrupt line; `<ip>.yml`: port, clock domain; `<ip>.rdl`: thanh ghi |
| Spec chữ | Agent trích, người duyệt | REQ-ID và câu yêu cầu, open item, quyết định |
| RTL và artifact khác | Phân tích tĩnh (pyslang) | Phân cấp module, port thật, instance, clock/reset thật, FSM |

### 4.2 Thực thể lõi

`project`, `block`, `module`, `port`, `interface`, `clock`, `reset`, `parameter`,
`register`, `field`, `interrupt`, `memory_region`, `requirement`, `decision`,
`open_item`, `test`, cộng với **quan hệ** giữa chúng (`implements`, `verifies`,
`connects`, `derives_from`). Pack mở rộng thêm, ví dụ pack analog thêm `pin_spec`.

Interface (APB, AXI, TileLink…) là **dữ liệu** trong thư viện interface, giống bus
definition của IP-XACT.

### 4.3 Tầng spec và ai làm gì

```
0. PRD             ← người nêu mục tiêu; AI phỏng vấn
1. HAS + chip.yml  ← AI đề xuất 2–3 kiến trúc, người chọn; hai file sinh cùng lúc
2. MAS + <ip>.yml + <ip>.rdl  ← AI soạn, Critic soi, chủ IP duyệt
3. RTL, test, ...  ← build graph
```

| Loại thông tin | Ví dụ | Ai làm |
|---|---|---|
| Quyết định | Chọn IP, tần số clock, nguồn NMI | AI đưa phương án, **người chọn** |
| Dữ kiện có sẵn | Số interrupt, bus của IP vendor | AI đọc và điền, **code đối chiếu RTL** |
| Suy ra được | Địa chỉ slave, số thứ tự interrupt | **Code tính** |

### 4.4 Check chéo deterministic

| Check | Bắt được |
|---|---|
| Schema | Thanh ghi thiếu reset value, port thiếu clock domain |
| Chéo IP và chip | Trùng địa chỉ; số interrupt không khớp; clock domain không tồn tại |
| Spec và RTL | Port trong spec khác port RTL thật |
| Truy vết | REQ không có test; RTL không trỏ về REQ |
| Kết nối | Nối dây top khác contract |

**Giá trị ngay cả khi chưa dùng AI:** `chipgraph ingest` dựng model từ dự án có sẵn rồi chạy
check chéo. Việc này an toàn, không sinh code, và phát hiện lệch ngay.

### 4.5 Lưu trữ và truy vấn

- **Nguồn:** file YAML/RDL/MD trong VCS của dự án, người đọc và sửa được.
- **Index:** `.chipgraph/state/cache/model.db` (SQLite), sinh lại được, không commit.
- **Truy vấn qua tool có kiểu:** `model.block("timer")`, `model.trace("REQ-TIM-004")`,
  `model.find(kind="port", clock="peri")`, `model.impact(change)`. Agent không tự grep
  file để suy ra số liệu.

### 4.6 Hình vẽ sinh từ model, không để AI vẽ

Học từ Archify (xem RESEARCH): agent **không vẽ**. Hình được sinh **deterministic** từ
Design Model, và model đã được đối chiếu với spec và RTL. Nhờ vậy hình không thể có khối
không tồn tại.

| Hình | Sinh từ |
|---|---|
| Block diagram chip, block diagram IP | `block`, `connects`, `interface` |
| Memory map | `memory_region` |
| Interrupt map | `interrupt` |
| Cây clock và reset | `clock`, `reset` |
| Phân cấp module | fact từ pyslang |

Rule `gen:diagram` ghi ra SVG/PNG và drawio. Đây là tổng quát hóa của `diagen.py` mà QSoC
đang dùng. Hình tự cập nhật khi model đổi, và PR có thể kèm hình diff.

### 4.7 Hướng dẫn cho mọi agent: `AGENTS.md` sinh ra

**Tùy chọn, không mặc định.** `chipgraph agents-md` sinh nội dung `AGENTS.md` (chuẩn mở của
Agentic AI Foundation, hơn 20 tool đọc được) từ profile và luật của tổ chức: lệnh check,
naming rule, cấu trúc thư mục, gate. Nhờ vậy kỹ sư dùng agent khác (Codex, Cursor…) vẫn
theo cùng luật.

- Nếu dự án **đã có** `AGENTS.md` hay `CLAUDE.md`, tool **không ghi đè**. Nó chỉ quản lý một
  đoạn nằm giữa `<!-- chipgraph:begin -->` và `<!-- chipgraph:end -->`; phần còn lại là của team.
- Mặc định in ra màn hình hoặc mở PR để team duyệt, không tự ghi thẳng vào repo.

### 4.8 Phát hiện mâu thuẫn, điểm vô lý, lệch nhau

Tool tìm vấn đề theo **5 lớp**, từ chắc chắn nhất đến chỉ là nghi ngờ. Mỗi phát hiện ghi rõ
nó đến từ lớp nào, để người biết nên tin tới đâu.

| Lớp | Tìm được gì | Ví dụ | Cơ chế | Độ chắc |
|---|---|---|---|---|
| **1. Cấu trúc và số liệu** | Lệch cứng giữa các artifact | Trùng địa chỉ; IP khai 4 interrupt nhưng contract có 2 line; port spec khác RTL; độ rộng hai đầu dây khác nhau; reset value trong MAS khác RTL; tín hiệu đi qua hai clock domain mà không qua bộ đồng bộ (CDC cấu trúc); số chung bị gõ tay | Check chéo deterministic trên Design Model (mục 4.4, 7.3) | **Chắc chắn** |
| **2. Mâu thuẫn trong spec** | Hai yêu cầu không thể cùng đúng; ràng buộc số không thỏa | REQ-4 "counter dừng ở 0" và REQ-9 "counter quay vòng"; clock 20 MHz không chia ra đúng baud 115200 trong sai số cho phép; FIFO quá nông so với tốc độ burst | **Chuẩn hóa yêu cầu**: REQ viết theo EARS được chuyển thành dạng có cấu trúc (sự kiện, điều kiện, đối tượng, hành động). Code so từng cặp REQ cùng đối tượng; ràng buộc số được giải bằng **z3** (SMT solver) | Cao với số liệu; trung bình với hành vi |
| **3. RTL làm sai spec** | Hành vi RTL khác yêu cầu | Ghi `LOAD` lúc đang chạy thì RTL nạp ngay, spec nói chờ hết chu kỳ | Test sinh độc lập từ spec (cocotb); SVA sinh từ REQ, chạy formal (SymbiYosys) ra counterexample | Cao, nếu test hoặc property phủ đến |
| **4. Thiết kế "vô lý"** | Đúng cú pháp, đúng spec, nhưng có mùi | Latch ngoài ý muốn, vòng tổ hợp, nhiều driver, reset không đồng bộ, output không có thanh ghi đi thẳng ra pad, port không dùng, clock gating trong RTL, FIFO rất sâu cho ngoại vi chậm | Lint và synth (Verilator, Yosys); **luật thiết kế của tổ chức** viết thành check; Critic đối chiếu với kho bài học và lỗi cũ trong knowledge base | Chắc với lint/synth; nghi ngờ với Critic |
| **5. Lệch quy trình** | Làm sai thứ tự hoặc thiếu | REQ không có test; test không trỏ về REQ; RTL build khi spec còn open item; duyệt gate trên hash cũ; file sinh ra bị sửa tay; artifact stale mà vẫn dùng | Build graph, truy vết, gate | **Chắc chắn** |

**Mỗi phát hiện (finding) có cùng một dạng:**

```yaml
id: F-0142
layer: 2                       # 1..5
severity: error                # error | warning | question
source: check:cross_chip       # hoặc critic, z3, formal, lint
evidence:                      # bắt buộc; không có bằng chứng thì không báo
  - doc/specs/QNSC_TIMER_MAS.md:118  (REQ-TIM-004)
  - doc/specs/QNSC_TIMER_MAS.md:131  (REQ-TIM-009)
claim: "REQ-TIM-004 và REQ-TIM-009 mâu thuẫn khi counter = 0 và AUTO_RELOAD = 0"
suggestion: "Chọn một hành vi; cập nhật REQ còn lại"
confidence: 0.9
status: open                   # open | fixed | waived
```

- **Bằng chứng là bắt buộc.** Critic (AI) phải chỉ ra file:dòng hoặc key trong model; không
  có bằng chứng thì phát hiện bị loại. Cách này giảm báo nhầm.
- **Phát hiện từ AI không bao giờ chặn build.** Nó ở mức `warning` hoặc `question`. Chỉ lớp
  deterministic (1, 5, lint, formal) mới được ở mức `error`.
- **Waiver:** người có thể bỏ qua một phát hiện, kèm lý do. Waiver gắn với hash; artifact đổi
  thì waiver hết hiệu lực và phát hiện được kiểm lại. Giống cách QSoC ghi "waived" cho
  CDC/RDC của INTMAP.
- **Chạy khi nào:** check deterministic chạy ở mọi bước build và mọi PR; Critic chạy trên
  phần đã đổi; `/audit` chạy toàn bộ dự án, dùng khi mới áp dụng tool cho dự án có sẵn hoặc
  trước mỗi mốc lớn.
- **Kết quả xem ở đâu:** `chipgraph findings`, comment trên PR, và dashboard.

**Giới hạn cần nói rõ:**
- Nếu spec sai **một cách nhất quán** (mọi nơi đều sai giống nhau), không check nào tự bắt
  được. Chỉ người duyệt spec, hoặc Critic so với kiến thức ngoài (chuẩn, datasheet), mới bắt.
- Lỗi vi kiến trúc phức tạp (deadlock trong giao thức, hiệu năng) cần formal sâu hoặc mô
  hình hiệu năng; tool chỉ gợi ý chỗ nghi ngờ.
- Timing và vật lý chỉ biết khi có backend (pack sau).
- Chất lượng lớp 2 và 4 phụ thuộc vào model và knowledge base; đo bằng evals có lỗi cài sẵn.

---

## 5. Agent

### 5.1 Vai là dữ liệu, không phải chương trình riêng

Một agent runtime chạy mọi vai. **Mặc định là runtime `claude-code`: agent chạy ngay trong Claude Code của chính người dùng, bằng gói Claude của họ** (mục 5.5). Runtime dùng API key (`claude-agent-sdk`, `generic` cho GLM hoặc model tự host) chỉ dùng khi cần (D35). Vai = prompt hệ thống + quyền +
hạng model. Khả năng theo loại artifact nằm ở **skill**, nạp khi cần.

| Vai | Làm | Quyền ghi | Hạng model mặc định |
|---|---|---|---|
| **Author** | Viết artifact: spec, RTL, TB, SVA, script, tài liệu (tùy skill) | Chỉ `outputs` của rule | vừa, leo lên mạnh khi fail |
| **Critic** | Soi spec hoặc diff với context mới; sinh câu hỏi, góp ý | Không ghi artifact | mạnh |
| **Planner** | Chia block thành module/task; đề xuất node mới cho graph | Chỉ file plan | mạnh |
| **Researcher** | Tìm, so sánh, luôn kèm nguồn | Chỉ file đề xuất | vừa |
| **Triage** | Phân loại lỗi, chọn hướng xử lý, tóm tắt log | Không ghi | nhỏ, hoặc lớp quyết định nhanh |

"Coder", "Tester", "Debugger", "Spec Writer" ở v0.1 là **Author với skill khác nhau**.
Ví dụ: debug là Author được giao lại task kèm kết quả check fail.

### 5.2 Vòng làm việc trong một rule agent

```
đọc input (Design Model + spec + skill)
  → ghi output
  → check ngay (P2) ── pass ──▶ trả kết quả có cấu trúc
        │ fail
        ▼
  Triage: lỗi hạ tầng? (thì retry, không tính lượt) / lỗi thiết kế? / spec mơ hồ? (thì hỏi người)
        ▼
  sửa (lượt 2, 3…; leo hạng model theo budget) ── hết ngân sách ──▶ dừng, viết HANDOFF
```

Kết quả trả về theo schema: file đã ghi, trạng thái, **giả định đã dùng**, câu hỏi mở.

### 5.3 Fan-out và gộp

| Luật | Nội dung |
|---|---|
| F1 | Chốt interface trước (từ Design Model), fan-out sau |
| F2 | Tập ghi của các nhánh không giao nhau; engine kiểm trước khi phát. File chung chỉ do `gen` rule hoặc một task tích hợp duy nhất ghi |
| F3 | Mỗi nhánh một workspace riêng do VCS adapter tạo (git: worktree; Perforce: client hoặc shelve riêng); engine merge; xung đột là lỗi |
| F4 | So **giả định** giữa các nhánh; lệch nhau thì hỏi người |
| F5 | Join luôn có check tích hợp (lint, sim mức block) |
| F6 | Best-of-N chọn bằng tool (pass test → ít cảnh báo → area), AI chỉ phân xử khi hòa |
| F7 | Nhánh fail chạy lại riêng; gộp theo thứ tự id cố định |

Có trần số nhánh chạy song song, theo rate limit API và license EDA.

### 5.4 Chọn model và chi phí

- **3 hạng**, map sang model cụ thể trong profile: nhỏ (Haiku 4.5), vừa (Sonnet 5),
  mạnh (Opus 5.5). Ở runtime `claude-code`, hạng được chuyển thành model của subagent
  (trong giới hạn gói Claude của người dùng).
- **Leo thang theo bậc** khi fail.
- **Lớp quyết định nhanh** `decide()` cho các câu hỏi dạng chọn đáp án (phân loại lỗi,
  có leo hạng không, có kẹt không, lệnh có rủi ro không). Thứ tự xử lý: luật bằng code →
  model nhỏ có structured output (hoặc Jev sau khi qua evals) → model mạnh khi độ tin cậy
  thấp.
- **Giảm token:**
  - context chung đặt đầu prompt để dùng prompt caching;
  - mỗi agent chỉ nhận slice Design Model của task mình;
  - log đưa vào agent đã được rút gọn;
  - việc code làm được thì không gọi AI.
- **Chọn model bằng evals** theo từng vai, không theo cảm giác.

### 5.5 Ai chạy model: Claude của chính người dùng

**Mặc định, mỗi kỹ sư dùng Claude của chính họ** (gói Pro, Max, Team hay Enterprise mà
công ty cấp). chipgraph **không giữ API key, không có đăng nhập riêng, không tự gọi model**
trong chế độ này.

```
Kỹ sư gõ /rtl timer trong Claude Code (đã đăng nhập bằng gói Claude của họ)
   │
   ▼
Plugin chipgraph ──MCP──▶ engine: "task tiếp theo là T03, vai Author, skill lang-sv/rtl,
   │                              input = slice Design Model, chỉ được ghi file X"
   ▼
Claude Code chạy subagent của vai đó (model, tool, quyền theo định nghĩa của vai)
   │   hook của plugin chặn mọi lần ghi ra ngoài file X
   ▼
subagent ──MCP──▶ engine.submit(T03): engine chạy check, ghi journal, quyết định bước sau
   │
   └─ lặp tới khi engine báo: gate, cần hỏi người, hoặc xong
```

- **Engine vẫn điều phối (P3):** engine quyết định task nào, vai nào, được ghi gì, check gì.
  Claude Code chỉ là nơi chạy model. Task độc lập được Claude Code chạy song song bằng
  nhiều subagent.
- **Chi phí** nằm trong gói Claude của người dùng hoặc công ty. Engine đếm số lượt và thời
  gian; số token lấy từ telemetry OpenTelemetry của Claude Code nếu được bật.
- **Tuân thủ điều khoản:** chipgraph là plugin chạy bên trong Claude Code (sản phẩm của
  Anthropic), và không tự làm đăng nhập claude.ai. Theo tài liệu Agent SDK, bên thứ ba
  không được đưa đăng nhập claude.ai vào sản phẩm riêng khi chưa được Anthropic duyệt, nên
  chipgraph **không** dùng gói Claude của người dùng qua Agent SDK.

**Khi nào mới cần API key:**

| Trường hợp | Runtime | Xác thực |
|---|---|---|
| Kỹ sư làm việc trong Claude Code (tương tác) | `claude-code` | **Gói Claude của người đó.** Không cần gì thêm |
| Chạy nền không mở Claude Code (`claude -p`) | `claude-code` (headless) | Gói Claude của người đó. Chính sách của Anthropic cho chế độ này đang thay đổi trong 2026 (credit riêng cho Agent SDK và `claude -p`, đã tạm dừng từ 2026-06-15), nên cần kiểm lại khi dùng |
| Bot CI trên GitHub | Claude Code GitHub Action | Token gói Claude (`claude setup-token`, lưu trong GitHub Secrets) **hoặc** API key của tổ chức |
| Dùng GLM | `claude-code` trỏ `ANTHROPIC_BASE_URL` sang Z.ai, hoặc runtime API | Gói hoặc key GLM của người dùng hay công ty |
| Air-gapped, model tự host | `generic` | Không cần key bên ngoài |
| Evals hằng đêm | `claude-code` headless hoặc `claude-agent-sdk` | Tài khoản dành riêng cho evals, có trần chi phí |

---

## 6. State, resume, thay đổi

### 6.1 Lưu ở đâu

Nguyên tắc P12: repo của dự án là của team, không phải của tool. Tool chia thứ nó cần lưu
thành 4 nhóm:

| Nhóm | Ví dụ | Mặc định | Tùy chọn |
|---|---|---|---|
| **Sản phẩm thiết kế** | Spec, RTL, test, plan, hình | Trong repo, qua PR như code do người viết | — |
| **Cấu hình** (nhỏ, người viết, có review) | `.chipgraph.yml` | **Trong repo**, một file duy nhất (mục 8.4) | `--profile` cho repo không được sửa (lối thoát nâng cao) |
| **Quyết định cần audit** | Duyệt gate, waiver | Gate nặng: **chính review của PR** (không thêm file). Gate nhẹ và waiver: **mỗi quyết định một file nhỏ** trong `.chipgraph/decisions/` (người đọc được, gắn hash; một file cho mỗi quyết định để hai người duyệt cùng lúc không bị conflict) | Lưu ở state backend (bên dưới) nếu team không muốn file này trong repo |
| **State chạy máy** | Journal, run, `HANDOFF.md`, cache, index model, finding thô, trace | **Không vào repo.** Thư mục `.chipgraph/state/` ở máy, bị gitignore | **State backend** cấu hình được: branch mồ côi `chipgraph-state` (giống `gh-pages`), một repo state riêng, hoặc server sau này. Dùng khi cần resume trên máy khác hay chia sẻ run trong team |

```yaml
# trong .chipgraph.yml
state:
  backend: local                 # local | branch:chipgraph-state | repo:git@…/chip_x-state.git
decisions:
  store: repo                    # repo (.chipgraph/decisions/<id>.yml) | state
```

**Thử mà không để lại dấu vết:** `chipgraph try` chạy các lệnh chỉ đọc trên một repo mà
**không ghi file nào** vào repo đó. Đây là cách thử tool trên dự án có sẵn trước khi team
quyết định áp dụng (mục 8.4).

**Không bao giờ ghi vào:** đường dẫn `write: deny` (IP vendor, PDK), repo mà người chạy
không có quyền, và repo được khai báo `readonly: true` trong workspace.

### 6.2 Resume

- Journal chỉ ghi thêm. State được dựng lại từ journal cộng với hash của artifact.
- Mỗi lần gọi tool có **khóa idempotency**, nên resume không chạy lại sim hay synth đã xong.
- Một run chạy trên một branch; một block chỉ có một run ghi tại một thời điểm (khóa).
- Lệnh: `status`, `resume`, `rewind <step>`.
- Mỗi lần dừng, tool viết `HANDOFF.md` gồm: đã làm, đang chờ ai, câu hỏi mở, bước tiếp.
  Mọi phiên agent bắt đầu bằng thủ tục cố định: đọc state và handoff, chạy `doctor`,
  smoke check.
- **Có bộ test resume:** cố ý ngắt ở từng loại bước rồi resume, kết quả phải giống chạy
  liền một mạch.

### 6.3 Thay đổi requirement hoặc spec

| # | Luật |
|---|---|
| C1 | Sửa ở tầng nguồn, rồi để graph lan xuống dưới |
| C2 | Không vá RTL khi spec chưa đổi. Lỗi do spec thì mở change đi ngược lên, sửa spec trước |
| C3 | `/impact` chia artifact thành 3 nhóm: **tự sinh lại** (`gen`), **cần làm lại** (`agent`/`human`, stale), **không ảnh hưởng**. Người duyệt rồi mới chạy |
| C4 | Chạy lại toàn bộ check liên quan, không chỉ phần đổi |
| C5 | Đóng băng theo giai đoạn; đổi tầng đã đóng băng cần người duyệt cấp cao hơn |

Ví dụ kiểm thử thật: diễn lại thay đổi "bỏ GPIO3" của QSoC, bảng tác động phải khớp với
những gì team đã làm tay.

### 6.4 Áp dụng cho dự án đang chạy (baseline)

Dự án có sẵn (như QSoC) đã có MAS, RTL, contract được team duyệt qua PR từ trước, nhưng
chưa có quyết định nào trong chipgraph. Không có bước này thì lệnh đầu tiên (`/rtl timer`)
sẽ dừng ở gate `spec:timer` vì "chưa duyệt".

- `chipgraph baseline` (chạy sau `init` và `ingest`): liệt kê các artifact trên nhánh chính
  kèm hash, và đề xuất coi chúng là **đã duyệt tại hash hiện tại**. Nguồn là lịch sử merge
  của VCS (đã qua PR).
- Lead xác nhận một lần. Tool ghi một quyết định `baseline` gắn hash của từng artifact.
- Từ đó mọi thay đổi đi theo luồng bình thường: artifact nào đổi thì stale và cần duyệt lại.
- Finding có sẵn lúc baseline (ví dụ từ `/audit`) được ghi nhận, không chặn build, và hiện
  trong báo cáo để team xử lý dần.

---

## 7. Tool, check, adapter

### 7.1 Agent xin khả năng, không xin tool

| Khả năng | Backend dùng lại | Kết quả chung |
|---|---|---|
| `lint` | Verilator, Verible (qua Edalize hoặc lệnh dự án) | `{status, issues[{file,line,rule,msg}]}` |
| `sim` | Verilator, Icarus, VCS, Xcelium, Questa (Edalize); testbench **cocotb** | `{status, tests[{name,pass,log_tail}]}` |
| `formal` | SymbiYosys | `{status, props[{name,result,cex}]}` |
| `synth` | Yosys, DC, Genus, Vivado (Edalize) | `{status, area, cells, warnings}` |
| `sta` | OpenSTA, PrimeTime | `{status, wns, tns, worst_paths}` |
| `parse` | pyslang | module, port, param, instance, FSM |
| `regs` | PeakRDL (RTL thanh ghi, C header, HTML) | file sinh ra + report |
| `wave` | đọc VCD/FST | tín hiệu quanh thời điểm lỗi |

- Adapter có thể chỉ là **lệnh của dự án** (`make lint BLOCK={block}`) cộng với bộ đọc log,
  để dự án có flow riêng dùng được ngay.
- Adapter có `runner: local | ssh | lsf | slurm`: tool thương mại chạy trên server có license.
- Mỗi adapter có bộ test chung: cùng input thì kết quả đúng schema chung.

### 7.2 Xuất ra ngoài

Engine mở các khả năng qua **MCP server**: `model.*`, `check.*`, `build`, `status`,
`answer_gate`, `ask`, và cho runtime `claude-code`: `next_task`, `get_context`, `submit`
(mục 5.5). Claude Code hay client MCP khác đều gọi được.

### 7.3 Check quy ước của dự án: naming, thư mục, file

**Có, và bắt buộc.** Agent sinh file rất nhanh, nên không có luật thì repo sẽ loạn trong
vài ngày. Quy ước được khai báo **dạng dữ liệu** trong profile, và pack `lang-sv` cùng
`spec-core` kiểm chúng như mọi check khác.

| Check | Kiểm gì | Cách làm |
|---|---|---|
| `naming` | Tên module, port, signal, parameter, instance, file theo naming rule của dự án | Luật là dữ liệu (tiền tố, hậu tố, regex theo **loại đối tượng**). pyslang cho biết tên nào là port, là thanh ghi hay là parameter, nên kiểm chính xác hơn regex trên text. Dự án đã có checker riêng thì dùng adapter |
| `layout` | File nằm đúng thư mục theo loại artifact; không có thư mục lạ | Profile khai báo mẫu đường dẫn cho từng loại: `design/{block}/rtl/`, `dv/{block}/`, `doc/specs/` |
| `filelist` | Mọi file RTL đều có trong filelist; không có file mồ côi; thứ tự package trước | So filelist với các file thật và với Design Model |
| `header` | Mỗi file có header đúng mẫu: module, mô tả, spec reference, REQ-ID | Template theo tổ chức |
| `generated` | File sinh ra không bị sửa tay | File sinh có dấu "generated" và hash; hash lệch thì báo |
| `vendor` | IP vendor không bị sửa ngoài bản vá đã khai báo | So với lock file (như `vendor.lock.yml` của QSoC) |
| `duplicate` | Không có hai module trùng tên, không có hai bản copy của cùng IP | Từ Design Model |
| `hardcode` | Số chung (địa chỉ, line interrupt) không được gõ tay trong RTL | So hằng số trong RTL với Design Model |

**Agent không tự đặt đường dẫn.** `outputs` của mỗi rule được sinh từ mẫu `layout` và
naming rule. Agent chỉ được ghi đúng các đường dẫn đó (mục 5.3, F2), nên file không thể
nằm sai chỗ hay sai tên.

```yaml
# trong .chipgraph.yml
naming:
  rules: org:qnsc/naming-v1.yml     # luật dạng dữ liệu của tổ chức
  checker: { use: rules }           # hoặc { use: cmd, cmd: "python3 flow/lint/naming_check.py {file}" }
layout:
  rtl:      "design/{block}/rtl/{module}.sv"
  wrapper:  "design/{block}/rtl/m_qnsc_wrap_{ip}.sv"
  filelist: "design/{block}/{block}.f"
  tb:       "dv/{block}/tb_{module}.py"
  spec:     "doc/specs/QNSC_{BLOCK}_MAS.md"
  generated: ["design/top/rtl/qnsc_pkg.sv", "doc/specs/docx/**"]
  vendor:   "vendor/**"
```

---

## 8. Tính tổng quát

### 8.1 Bốn lớp

| Lớp | Chứa | Luật |
|---|---|---|
| **Core** | Engine, Design Model framework, agent runtime, plugin API | Không có chữ QSoC, APB, GF180, Verilator |
| **Pack** | Rule, vai, skill, schema mở rộng, check, template, lệnh | Có manifest `pack.yml` (cung cấp gì, cần gì) |
| **Adapter** | Tool (Edalize, make…), format (IP-XACT, SystemRDL, hjson, YAML riêng), LLM, runner | Sau giao diện chung |
| **Dữ liệu dự án** | `.chipgraph.yml`, luật, template, spec | Mỗi dự án tự có |

CI của tool chặn core import pack hoặc adapter.

### 8.2 Pack và preset

| Pack | Nội dung |
|---|---|
| `spec-core` | Schema lõi, check chéo, `/intake`, `/arch`, `/mas`, `/impact` |
| `lang-sv` | Luật, skill, parser SystemVerilog |
| `digital-rtl` | Rule `rtl_module`, `wrapper`, `top` |
| `dv` | Rule `tb_module` (cocotb), `sva`, vòng coverage |
| `assist` | `/ask`, `/triage`, `/script` |
| `pm` | Tracker, báo cáo |
| Sau | `backend`, `firmware`, `fpga`, `optimize` (RL/Bayes, không LLM), `analog`, `lang-vhdl` |

| Preset | Pack bật sẵn |
|---|---|
| `mcu-soc` | spec-core, lang-sv, digital-rtl, dv, assist, pm (+ firmware, backend khi có) |
| `ip-block` | spec-core, lang-sv, digital-rtl, dv, assist |
| `fpga-prototype` | như `ip-block` + fpga |
| `analog-ip` (sau) | spec-core, analog, assist |

### 8.3 Cấu hình nhiều tầng

```
mặc định tool → tổ chức (qnsc) → preset → dự án (.chipgraph.yml) → đường dẫn → block
                                                        + người dùng (chỉ trải nghiệm, mục 8.6)
```

```yaml
# .chipgraph.yml của QSoC
project: qsoc
extends: [org:qnsc, preset:mcu-soc]   # org:<tên> = luật mở đi kèm tool (src/chipgraph/orgs/); luật riêng dùng git+https://…@tag
packs: [spec-core, lang-sv, digital-rtl, dv, assist, pm]
spec:
  chip: { path: util/qsoc_contract.yml, format: qsoc-contract }
  ip_dir: doc/specs
  requirements: { infer: verification }   # D37: tạm suy ra REQ từ mục Verification của MAS
adapters:
  lint:  { use: make, cmd: "make lint BLOCK={block}", parser: verilator }
  sim:   { use: edalize, tool: verilator }
  parse: { use: pyslang }
naming: { checker: { use: cmd, cmd: "python3 flow/lint/naming_check.py {file}" } }
layout: { rtl: "design/{block}/rtl/{module}.sv", filelist: "design/{block}/{block}.f" }   # đủ ở mục 7.3
target: { kind: asic, pdk: gf180 }
autonomy: { spec: L2, rtl: L3, verify: L3 }
runtime: claude-code               # mặc định: Claude của chính người dùng (mục 5.5)
models:                             # dùng cho runtime API (CI, evals) và chọn model của subagent
  providers:
    claude: { api: anthropic }
    glm:    { api: anthropic-compatible, base_url: "https://api.z.ai/api/anthropic" }
  tiers: { small: claude-haiku-4-5, medium: claude-sonnet-5, large: claude-opus-5-5 }
  alt:   { medium: glm }            # model thay thế, chọn theo evals (D24)
data: { default: internal, nda_paths: ["vendor/pdk/**"], nda_model: block }
reviewers: [nghia]
```

**Không trừu tượng hóa sớm:** điểm mở rộng có từ ngày đầu, nhưng adapter hay preset thứ
hai chỉ viết khi có dự án thật cần.

### 8.4 Cấu hình: một cách duy nhất

**Một repo dùng chipgraph thì có một file `.chipgraph.yml` ở gốc repo. Chỉ vậy.**

Đây là cách mọi tool dev quen thuộc đang làm (`pyproject.toml`, `.pre-commit-config.yaml`,
`.editorconfig`), nên người dùng không phải học thêm khái niệm nào.

| Nhu cầu | Cách làm, vẫn chỉ với file đó |
|---|---|
| Dùng luật chung của công ty cho nhiều repo | Một dòng `extends: [git+https://…/company-rules@v3]`. Luật chung sửa một chỗ; mỗi repo chỉ ghi phần riêng |
| Thử tool trước khi áp dụng | `chipgraph try`: tự suy ra profile, **chỉ chạy lệnh đọc** (`ask`, `audit`), không ghi file nào. Thấy ổn thì `chipgraph init` ghi `.chipgraph.yml` từ kết quả đó, người duyệt trước khi commit |
| Luật đổi cùng code | Tự nhiên: cùng commit, cùng PR |

**Lối thoát nâng cao, không nằm trong hướng dẫn chính:** repo không được phép sửa (vendor,
khách hàng, Perforce bị khóa) thì dùng `--profile <đường dẫn>`; trong workspace thì profile
của repo `readonly` nằm ở repo manifest. Người dùng bình thường không cần biết cách này.

**Đã bỏ:** tự tra profile từ một repo cấu hình trung tâm theo URL của repo. Cách đó ẩn, khó
đoán ("cấu hình này từ đâu ra?"), và gây rối cho người dùng (D33).

### 8.5 Áp dụng được cho dự án nào, hôm nay

**Kiến trúc** mở rộng được cho mọi loại dự án IC. **Nội dung** (pack) hiện mới được thiết
kế cho digital front-end. Bảng dưới là tình trạng thật:

| Lĩnh vực | Tình trạng | Ghi chú |
|---|---|---|
| Spec, kiến trúc (PRD, HAS, MAS, contract) | **Có trong design** | `spec-core` |
| RTL digital, wrapper, top | **Có trong design** | `lang-sv`, `digital-rtl` |
| Verification: testbench, SVA, formal, coverage | **Có trong design** | `dv` |
| Hỏi đáp, triage, script, báo cáo | **Có trong design** | `assist`, `pm` |
| Lint, CDC, RDC | Một phần | Lint có; CDC/RDC cần tool (SpyGlass, Questa CDC hoặc open-source hạn chế) và một pack `signoff-rtl` |
| Low-power (UPF) | Chưa thiết kế | Cần entity `power_domain` và adapter tool đọc UPF |
| DFT (scan, MBIST, ATPG) | Chưa thiết kế | Pack `dft`; phụ thuộc tool thương mại |
| Backend (synth, PnR, STA, DRC/LVS) | Mới ở mức ý tưởng | Pack `backend`, `optimize`; đọc report là bước đầu |
| FPGA | Mới ở mức ý tưởng | Preset `fpga-prototype` |
| Firmware, đồng kiểm HW/SW | Mới ở mức ý tưởng | Pack `firmware`: header, driver từ register map |
| Analog, mixed-signal | **Chưa thiết kế** | Artifact khác hẳn (schematic, netlist, layout); tool Virtuoso, SPICE; cần Design Model mở rộng riêng |
| An toàn chức năng (ISO 26262), bảo mật phần cứng | Chưa thiết kế | Cần entity như `safety_req`, FMEDA |
| Post-silicon, bring-up | Chưa thiết kế | |

**Nói thẳng:** design **chưa được kiểm chứng**. Nó là giả thuyết tốt nhất hiện nay, và
chỉ được chứng minh khi M0–M2 chạy được trên block thật của QSoC, rồi M5 chạy được trên
dự án loại thứ hai.

### 8.6 Mỗi dự án, mỗi người một cách làm

Mỗi công ty, dự án và kỹ sư có template, cách đặt tên, cách tổ chức thư mục và code riêng.
Tool **không áp một cách làm cố định**. Nó học và tuân theo cách làm của dự án.

**Luật phân quyền giữa các tầng:**

| Tầng | Được quyết | Không được quyết |
|---|---|---|
| Tổ chức | Luật chung (naming, template tài liệu, chính sách dữ liệu, người duyệt) | — |
| Dự án | Layout, naming, template, lệnh check, flow, gate; được ghi đè luật tổ chức nếu tổ chức cho phép | — |
| Đường dẫn trong dự án (`paths:`) | Ngoại lệ cho một vùng: code legacy, IP vendor, thư mục của một nhóm | Nới lỏng luật bảo mật dữ liệu |
| **Người dùng** (`~/.config/chipgraph/user.yml`) | Trải nghiệm cá nhân: mức tự động cao hay thấp hơn (trong giới hạn dự án), kênh thông báo, ngôn ngữ trả lời, model ưa dùng (trong danh sách dự án cho phép), editor | **Không được đổi quy ước của artifact** (naming, layout, template, check). Nếu mỗi người một kiểu thì repo loạn |

Thứ tự ghép đầy đủ:

```
mặc định tool → tổ chức → preset → dự án → đường dẫn → block        (quy ước artifact)
                                                    + người dùng    (chỉ trải nghiệm cá nhân)
```

**6 cơ chế cho sự linh hoạt:**

| # | Cơ chế | Nghĩa là |
|---|---|---|
| V1 | **Học quy ước từ dự án có sẵn**: `chipgraph learn` | Quét repo, suy ra mẫu thư mục, naming (regex theo loại đối tượng qua pyslang), mẫu header, kiểu filelist, template MAS; đề xuất một profile nháp kèm thống kê độ phủ ("97% port có tiền tố `i_`/`o_`"). Người duyệt rồi mới dùng |
| V2 | **Template ghi đè từng phần** | Template dùng Jinja2, chia thành block. Dự án chỉ ghi đè block cần đổi (ví dụ header), không phải copy cả file. Thứ tự tìm: block → dự án → tổ chức → pack |
| V3 | **Code mẫu của chính dự án** (`style.exemplars`) | Profile chỉ ra vài file "chuẩn" của dự án; Author nhận chúng làm ví dụ, nên code sinh ra giống phong cách của team. Formatter của dự án (verible-format, emacs AUTO, script riêng) chạy sau khi sinh |
| V4 | **Ngoại lệ theo đường dẫn** (`paths:`) | Vùng legacy hay IP vendor dùng luật khác hoặc được miễn check, có lý do ghi rõ |
| V5 | **Plugin cục bộ của dự án** (`.chipgraph/plugins/`) | Khi quy ước quá đặc biệt để khai báo bằng dữ liệu (ví dụ đường dẫn tính theo công thức riêng), dự án viết một hàm Python nhỏ theo plugin API mà không phải fork tool. Plugin cục bộ chạy code, nên phải được lead duyệt như code |
| V6 | **Chia sẻ quy ước** | Luật tổ chức và preset nằm trong repo git riêng: `extends: [git+https://…/company-rules@v3]`. Nhiều dự án dùng chung, có version |

```yaml
# ví dụ trong .chipgraph.yml
extends: [git+https://github.com/company_x/chip-rules@v3, preset:ip-block]
layout:
  rtl: "hw/{block}/src/{block}_{module}.v"        # dự án này dùng Verilog-2001, thư mục hw/
templates:
  override: { rtl_header: templates/header.j2 }   # chỉ thay phần header
style:
  exemplars: [hw/uart/src/uart_tx.v, hw/spi/src/spi_core.v]
  formatter: { use: cmd, cmd: "verible-verilog-format --inplace {file}" }
paths:
  "hw/legacy/**": { checks: { naming: off }, reason: "code cũ từ chip v1, không sửa" }
  "hw/vendor/**": { checks: { naming: off, header: off }, write: deny }
plugins: [.chipgraph/plugins/layout_rules.py]
```

```yaml
# ~/.config/chipgraph/user.yml (cá nhân, không commit)
autonomy: { rtl: L2 }          # người này muốn duyệt từng bước, thấp hơn mức L3 của dự án
notify: slack
answer_language: vi
```

**Kiểm tính nhất quán của chính cấu hình:** `chipgraph config check` báo xung đột (hai mẫu
layout trùng nhau, luật naming mâu thuẫn, người dùng cố vượt giới hạn dự án), và
`config show --explain` cho biết mỗi giá trị đến từ tầng nào.

### 8.7 Dự án có nhiều repo (workspace)

Nhiều team chia một chip ra nhiều repo: mỗi IP một repo, một repo tích hợp top, một repo
verification, repo spec, repo firmware, repo PDK hoặc vendor. Tool gom chúng thành một
**workspace**.

```
workspace "chip_x"  (khai báo trong repo hub: repo top, hoặc một repo chỉ chứa manifest)
  ├─ soc-top      role: top       blocks: [top, intmap]     ref: main
  ├─ ip-uart      role: ip        blocks: [uart]            ref: v1.2.0
  ├─ ip-spi       role: ip        blocks: [spi]             ref: v0.9.1
  ├─ dv-soc       role: dv                                  ref: main
  ├─ chip-spec    role: spec      (HAS, chip.yml)           ref: main
  └─ fw-sdk       role: firmware  (VCS: git)                ref: main
      vendor-ip   role: vendor    (VCS: perforce)           ref: @4521
```

**Cơ chế:**

| # | Cơ chế | Nghĩa là |
|---|---|---|
| W1 | **Manifest workspace** | File `chipgraph-workspace.yml` nằm ở **repo hub**: liệt kê repo thành viên, vai trò, block sở hữu, **ref khóa** (tag, commit). Hub thường là repo top; team nào không muốn đụng repo top thì tạo một repo chỉ chứa manifest (vẫn gọi là hub, giống manifest của Google `repo`). Mỗi repo thành viên có `.chipgraph.yml` riêng như bình thường. Chỉ repo `readonly` (vendor, khách hàng) mới để profile ở hub |
| W2 | **Dùng lại cách team đang ghép repo** | Nếu team đã dùng git submodule, Google `repo`, Zephyr `west`, FuseSoC, **Bender** (PULP dùng), hay manifest vendor kiểu QSoC (`vendor/manifest.yml`), tool đọc chúng qua adapter thay vì bắt viết manifest mới |
| W3 | **Design Model trải qua nhiều repo** | Mỗi fact ghi rõ nguồn: repo, đường dẫn, commit. Check chéo chạy **giữa các repo**: port của IP ở `ip-uart@v1.2.0` so với cách `soc-top` nối dây; interrupt trong `chip.yml` so với spec IP |
| W4 | **Stale lan qua repo** | IP đổi port ở bản mới thì phần nối dây trong top và test trong `dv-soc` thành stale. Tool báo **lệch version**: top đang dùng `ip-uart@v1.2.0`, trong khi IP đã có `v1.3.0` đổi interface |
| W5 | **Một thay đổi, nhiều PR có liên kết** | `/change "UART thêm 1 interrupt"` tạo một change ID và các PR theo đúng thứ tự: IP trước (tag mới), rồi `chip-spec` (contract), rồi `soc-top` (nâng ref + nối dây), rồi `dv-soc`. Gate của change chỉ đóng khi mọi PR đã merge |
| W6 | **Tôn trọng quyền từng repo** | Agent chỉ ghi vào repo mà người chạy có quyền. PR vào repo của nhóm khác cần chủ repo đó duyệt (CODEOWNERS, reviewer) |
| W7 | **State hai cấp** | State chạy máy của mỗi repo nằm ở state backend (mục 6.1), không nằm trong repo. Change request liên repo và finding liên repo nằm ở state backend của workspace; quyết định cần audit liên repo được ghi vào `.chipgraph/decisions/` của hub |
| W8 | **CI hai cấp** | CI của mỗi repo chạy check của repo đó. CI của hub chạy check tích hợp liên repo khi có PR nâng ref, và hằng đêm |
| W9 | **Nhiều VCS trong một workspace** | Mỗi thành viên có VCS adapter riêng (git, Perforce…) |

**Cách dùng:**

```bash
# Lead hoặc người tích hợp, một lần, trong repo hub
chipgraph workspace init
chipgraph workspace add git@github.com:company_x/ip-uart.git --role ip --blocks uart --ref v1.2.0
chipgraph workspace sync            # clone hoặc cập nhật mọi repo vào thư mục workspace
chipgraph ingest --workspace        # Design Model của cả chip
chipgraph audit --workspace         # tìm lệch giữa các repo

# Kỹ sư chỉ làm một IP: làm việc trong repo IP như bình thường
cd ip-uart && chipgraph rtl uart    # profile có `workspace: git+https://…/soc-top@main`
                                    # để đọc contract và luật chung, không cần clone cả workspace

# Thay đổi chạm nhiều repo
chipgraph change "UART thêm interrupt RX timeout"
#  -> /impact liệt kê repo và file bị ảnh hưởng
#  -> sau khi duyệt: PR ip-uart, PR chip-spec, PR soc-top (nâng ref), PR dv-soc, cùng change ID
```

Một repo duy nhất (monorepo, như QSoC) là trường hợp riêng: workspace có đúng một thành viên.
Không cần khai báo gì thêm.

---

## 9. Độ tin cậy, an toàn, chất lượng

| Nhóm | Cơ chế |
|---|---|
| Sandbox | Runtime `claude-code`: quyền và sandbox của Claude Code, cộng hook của plugin (chặn ghi ngoài `outputs`, chặn `git push`), và `submit` kiểm lại diff. Runtime API: sandbox có chính sách khai báo (container, hoặc NVIDIA OpenShell khi ổn định). Cả hai: chỉ ghi `outputs`, mạng chỉ mở cho Researcher, có audit trail |
| Dữ liệu | Nhãn `public`/`internal`/`nda` theo đường dẫn; LLM adapter chặn file `nda` hoặc chuyển sang model local; không đưa nội dung mật vào truy vấn web |
| Ngân sách | Trần số lượt, thời gian (và token hoặc tiền khi đo được) cho rule, run, tháng. Chế độ `claude-code`: giới hạn thật là gói Claude của người dùng |
| Kẹt | Cùng lỗi lặp lại 2 lần, hoặc diff dao động, thì dừng và báo người |
| Lỗi hạ tầng | Retry có backoff, timeout, chờ license; không tính là lỗi thiết kế |
| Không chắc | Agent đánh dấu giả định; chỗ quan trọng sinh 2–3 bản độc lập rồi so |
| Quan sát | Trace theo chuẩn OpenTelemetry GenAI; `chipgraph report` |
| Tái lập | Manifest mỗi run: version model, pack, adapter, tool EDA, hash input |
| Môi trường | `chipgraph doctor`: tool, version, license, Claude Code đã đăng nhập (hoặc API key khi dùng runtime API), profile hợp lệ |
| Học từ người | Lý do sửa hoặc từ chối ở gate được ghi lại, gom thành đề xuất sửa skill; người duyệt mới áp dụng |
| Kiểm tool | Unit test core; test chung cho adapter; test đầu-cuối trên dự án mẫu; **evals** (Inspect AI) chạy trong CI của tool mỗi khi đổi prompt, pack, model |
| Phân loại lỗi của harness | Mỗi lần agent fail hay bị người sửa được gắn một nhãn: **context** (thiếu thông tin), **constraint** (làm điều không được làm), **verification** (sai mà không check nào bắt), **planning** (chia việc sai), hoặc **infra**. Mỗi nhãn chỉ ra chỗ phải sửa: context thì sửa Design Model và skill; constraint thì thêm quyền hoặc luật; verification thì thêm check; planning thì sửa Planner hoặc rule. `chipgraph report` cho thấy phân bố nhãn theo thời gian |

**Thước đo "work well":**
- tỉ lệ rule pass không cần người sửa;
- số lượt sửa trung bình;
- tỉ lệ bị từ chối ở gate và lý do;
- lỗi lọt qua gate;
- token, tiền, thời gian mỗi block;
- tỉ lệ resume thành công;
- thời gian người bỏ ra so với làm tay.

---

## 10. Cách dùng

### 10.1 Cài và bắt đầu

**Một lần trên mỗi máy:** cài `uv`, cùng các tool EDA dự án đang dùng. Sau đó, trong Claude Code:

```
/plugin marketplace add quynhonsemiconductor/chipgraph
/plugin install chipgraph
```

Plugin tự chạy engine và MCP server bằng `uvx chipgraph`, nên không phải cài CLI riêng.
Muốn dùng CLI ngoài Claude Code (CI, script) thì chạy `uv tool install chipgraph`.

**Một lần cho mỗi repo:**

```
cd <repo của dự án> && claude
/chipgraph:try          # tùy chọn: xem trước, chỉ đọc, không ghi file
/chipgraph:init         # tạo .chipgraph.yml → người duyệt → commit
/chipgraph:ingest       # dựng Design Model từ spec, RTL đang có
/chipgraph:baseline     # repo đã có sẵn: ghi nhận artifact hiện tại là đã duyệt (mục 6.4)
```

**Hằng ngày:** `cd` vào repo rồi mở `claude`. Gõ lệnh (`/chipgraph:rtl timer`, `/chipgraph:ask …`),
hoặc nói bằng lời ("viết RTL cho timer theo MAS"); skill của plugin tự nhận ra việc cần làm.
Trong tài liệu này lệnh được viết gọn, bỏ tiền tố (`/rtl`, `/ask`…); tên đầy đủ trong
Claude Code có tiền tố `chipgraph:`.

### 10.2 Ba chế độ

| Chế độ | Ví dụ |
|---|---|
| Tương tác trong Claude Code | `/mas timer`: AI hỏi, soạn, người sửa ngay |
| Tự chạy tới gate | `/rtl timer`: build target, chỉ dừng ở gate hoặc khi cần hỏi |
| Bot CI | PR mở thì `chipgraph check` + `review` comment; PR đổi contract thì comment `/impact` |

**Ai chạy model:** mặc định là **Claude của chính người dùng**, chạy trong Claude Code
(mục 5.5). Chế độ nào cũng dùng cùng engine, cùng rule, cùng check và cùng gate, nên kết
quả giống nhau; chỉ khác chỗ model chạy.

### 10.3 Mức tự động

| Mức | Nghĩa | Mặc định cho |
|---|---|---|
| L1 | Gợi ý, trả lời, người tự làm | Quyết định kiến trúc |
| L2 | Soạn nháp, người duyệt từng bước | PRD, HAS, MAS |
| L3 | Tự chạy tới gate | RTL, TB, SVA, debug |
| L4 | Tự chạy cả workflow, người chỉ duyệt PR cuối | Việc lặp lại đã có evals tốt |
| L5 | Tự merge | **Không dùng** |

Thang này tương tự cách ngành mô tả mức tự động (L1–L5), nhưng định nghĩa ở đây là của
tool này.

### 10.4 Lệnh

| Nhóm | Lệnh |
|---|---|
| Hiểu | `/ask`, `/status`, `/trace <REQ>`, `/impact <change>` |
| Spec | `/intake`, `/arch`, `/mas <ip>`, `/spec-check` |
| Build | `/plan <ip>`, `/rtl <ip>`, `/wrap <ip>`, `/verify <ip>`, `/review` |
| Hỗ trợ | `/triage`, `/script`, `/research` |
| Bắt đầu | `init`, `try` (chỉ đọc, không ghi file), `learn`, `ingest`, `baseline`, `config check`, `config show --explain`, `doctor`, `agents-md` (tùy chọn) |
| Phát hiện | `/audit`, `findings`, `waive` |
| Vận hành | `/resume`, `/change`, `/report`, `/eval`, `workspace init / add / sync` |

### 10.5 Giao diện theo dõi

Giao diện chỉ là cửa sổ nhìn vào state trong git. Làm theo bậc:
1. terminal, `HANDOFF.md`, GitHub, Langfuse;
2. trang tĩnh sinh ra (`chipgraph site`);
3. dashboard web: ma trận block × stage, hộp chờ người, dòng thời gian run, truy vết,
   tác động, chất lượng và chi phí;
4. extension VS Code.

---

## 11. Công nghệ và dùng lại

| Phần | Chọn | Lý do |
|---|---|---|
| Ngôn ngữ | **Python 3.14** (hỗ trợ từ 3.13) (uv, pydantic, asyncio, typer); TypeScript chỉ cho frontend dashboard sau này | Hệ sinh thái EDA và agent SDK ở Python; tải là I/O, không phải CPU (D21) |
| Engine | Tự viết: scheduler graph + SQLite | Nhỏ, kiểm soát hoàn toàn (D3, D9) |
| Agent runtime | **`claude-code` (mặc định: Claude Code của người dùng, qua plugin + MCP)**; `claude-agent-sdk` (API key); `generic` (API tương thích OpenAI) | Dùng gói Claude sẵn có, không cần key (D35); CI, evals, model tự host (D10, D27) |
| Tool EDA | **Edalize** + lệnh dự án | Đã hỗ trợ nhiều simulator, synth, FPGA |
| Parse RTL | **pyslang** | Parser SystemVerilog đầy đủ |
| Thanh ghi | **SystemRDL + PeakRDL** | Chuẩn Accellera; sinh RTL, header, tài liệu |
| Testbench | **cocotb** | Python dễ cho LLM viết; chạy trên nhiều simulator |
| Formal | SymbiYosys | Open-source |
| Ràng buộc số trong spec | z3 (`z3-solver` 5.1) | SMT solver chuẩn; kiểm ràng buộc như chia clock, căn địa chỉ, độ sâu FIFO |
| Sandbox | Claude Code (runtime mặc định) + hook; container hoặc OpenShell cho runtime API | Không tự viết (D11, D35) |
| Trace | OpenTelemetry GenAI + Langfuse | Chuẩn chung, tự host được |
| Evals | Inspect AI | MIT, hỗ trợ agent dùng tool |
| Giao diện | Claude Code plugin + CLI; web sau | Team đang dùng Claude Code |

### 11.1 Phiên bản tham chiếu (kiểm ngày 2026-09-27)

Kiểm trên PyPI, GitHub Releases và endoflife.date. Đây là mốc lúc bắt đầu, **không phải
version gắn cứng**: version thật được khóa trong `uv.lock` và Docker image, và Renovate mở
PR nâng version (mục 16, O7).

| Thành phần | Bản mới nhất | Phát hành | Ghi chú |
|---|---|---|---|
| Python | **3.14.7** | nhánh 3.14 từ 2025-10 | Dùng 3.14, hỗ trợ từ 3.13. pyslang, cocotb, pydantic đều có wheel cp314. Python 3.15 ra khoảng 2026-10; nâng khi các thư viện có wheel |
| uv | 0.12.19 | 2026-09-25 | |
| claude-agent-sdk | 0.2.160 | 2026-09-25 | **Chưa tới 1.0 và đổi rất nhanh**: khóa version, bọc sau runtime adapter |
| anthropic (SDK) | 1.8.0 | 2026-09-22 | |
| mcp (Python SDK) | 2.2.0 | 2026-09-07 | MCP SDK v2 |
| zai-sdk (GLM) | 0.2.3 | 2026-06-16 | Hoặc dùng endpoint tương thích Anthropic |
| pydantic | 2.13.5 | 2026-08-28 | |
| typer | 0.27.2 | 2026-08-28 | |
| pyslang | 11.0.0 | 2026-05-15 | |
| cocotb | 2.1.0 | 2026-08-30 | cocotb 2.x (API mới so với 1.x) |
| edalize | 0.6.8 | 2026-04-24 | |
| fusesoc | 2.4.7 | 2026-09-08 | Tham khảo cho thư viện IP |
| systemrdl-compiler / peakrdl / peakrdl-regblock | 1.32.2 / 1.5.0 / 1.3.1 | 2026-02 / 2025-10 / 2026-03 | Ổn định, ít đổi |
| inspect-ai | 0.3.271 | 2026-09-26 | Đổi nhanh, cần khóa version |
| opentelemetry-sdk | 1.45.0 | 2026-09-25 | |
| langfuse (SDK / server) | 4.15.6 / v4.46.0 | 2026-09-24 / 2026-09-25 | |
| NVIDIA OpenShell | v0.1.1 (Apache-2.0) | 2026-09-26 | **Còn rất mới (0.1)**; giữ container làm phương án chính cho tới khi ổn định (D11) |
| ruff / mypy / pytest | 0.16.9 / 2.3.1 / 9.1.1 | 2026-09 / 2026-08 / 2026-06 | |
| Verilator | v5.052 | 2026 | QSoC đang dùng 5.020; adapter và `doctor` phải xử lý khác biệt giữa các version |
| Verible | v0.0-4296 | 2026-09-23 | |
| Yosys | v0.69 | 2026-09-09 | |
| Icarus Verilog | v13.0 | 2026-03-02 | |
| OSS CAD Suite | bản build theo ngày (2026-09-27) | hằng ngày | **Nền cho Docker image CI**: có sẵn Yosys, SymbiYosys, Icarus, Verilator…; khóa theo ngày build |

---

## 12. Triển khai và hạ tầng

**Local-first: bắt đầu không cần host server nào.** Tool chạy trên máy kỹ sư và trong CI.
Dịch vụ bên ngoài duy nhất là API model AI.

### 12.1 Cái gì chạy ở đâu

| Thành phần | Chạy ở | Ghi chú |
|---|---|---|
| CLI `chipgraph`, engine, MCP server | **Máy kỹ sư** (macOS, Linux) | Plugin tự chạy bằng `uvx chipgraph`; `uv tool install chipgraph` khi cần CLI ngoài Claude Code |
| Plugin Claude Code | Máy kỹ sư, trong Claude Code | Cài từ marketplace là repo GitHub của org |
| Tool EDA open-source | Máy kỹ sư, hoặc Docker image | Verilator, Verible, Icarus, Yosys, SymbiYosys, như QSoC đang dùng |
| Tool EDA thương mại | **Server có license** (sẵn có của trường hoặc công ty) | Engine gọi qua runner `ssh`/`lsf`/`slurm`; không cài tool thương mại lên máy kỹ sư |
| Artifact, cấu hình, quyết định | **Repo của dự án** (qua PR) | Chỉ những gì team muốn giữ cùng thiết kế (mục 6.1) |
| State chạy máy (journal, run, handoff) | **State backend**: mặc định thư mục cục bộ bị gitignore | Tùy chọn branch mồ côi hoặc repo state riêng; không cần database server |
| Cache, index Design Model | Máy kỹ sư (`.chipgraph/state/cache/`) | Sinh lại được |
| Model AI | **Claude của chính người dùng** qua Claude Code (mặc định); GLM (Z.ai); API key của tổ chức chỉ cho CI và evals; model tự host khi cần (mục 5.5, 12.6) | chipgraph không giữ credential của người dùng |
| Bot CI | **GitHub Actions** | Check deterministic chạy trong Docker image, không cần model. Review bằng AI dùng Claude Code GitHub Action với token gói Claude hoặc API key, lưu trong GitHub Secrets |

### 12.2 Phát hành tool

| Thứ | Cách |
|---|---|
| Mã nguồn | Repo **public** `quynhonsemiconductor/chipgraph`, Apache-2.0 (D23) |
| CLI | Phát hành lên PyPI (`chipgraph`), có version tag (semver); plugin gọi `uvx chipgraph@<version>` để khóa version |
| Plugin | Chính repo đó làm Claude Code plugin marketplace |
| Docker image cho CI | `ghcr.io/quynhonsemiconductor/chipgraph-eda:<version>`: dựa trên OSS CAD Suite (khóa theo ngày build) + Python 3.14 + CLI |
| Pack và preset | Đi kèm CLI; pack của tổ chức hoặc dự án có thể để trong repo riêng |

### 12.3 Chỉ host thêm khi cần

| Khi nào | Host gì | Ở đâu |
|---|---|---|
| M4: cần xem trace của cả team | Langfuse (open-source), dùng `docker compose` | Một VM hoặc server nội bộ nhỏ; hoặc bỏ qua, chỉ dùng `chipgraph report` |
| M4: trang trạng thái | Trang tĩnh `chipgraph site` | GitHub Pages (repo private thì dùng Pages nội bộ) |
| M5: dashboard có thao tác | Web app nhẹ (FastAPI + frontend tĩnh), đọc state từ git, đăng nhập bằng GitHub | VM nội bộ hoặc cloud nhỏ |
| Có dữ liệu NDA không được gửi cloud | Server model local có GPU | Server nội bộ; LLM adapter trỏ vào đó |
| Nhiều người chạy dài cùng lúc | Worker chung, có thể dùng Temporal (D9) | Chỉ khi thật sự cần |

### 12.4 Chi phí vận hành

| Giai đoạn | Chi phí chính |
|---|---|
| M0–M3 | Gói Claude sẵn có của kỹ sư (không thêm chi phí tool); GitHub Actions minutes; token cho CI và evals nếu bật; không có server |
| M4–M5 | Thêm một VM nhỏ nếu tự host Langfuse hoặc dashboard |
| Khi có NDA | Server GPU cho model local (nếu chính sách bắt buộc) |

### 12.5 An toàn khi triển khai

- API key chỉ nằm trong keychain của máy hoặc GitHub Secrets. Không bao giờ nằm trong repo
  hay log.
- **Repo public:** không có dữ liệu nội bộ nào trong repo tool. Ví dụ và evals chỉ dùng dữ
  liệu open-source (IP open, PDK open như GF180, Sky130). Profile và dữ liệu của dự án thật
  nằm ở repo dự án, không nằm ở repo tool. Có `SECURITY.md` và secret scanning.
- Bot CI chạy với quyền tối thiểu: đọc repo, comment PR. Không có quyền merge.
- Dữ liệu gửi lên model AI tuân theo nhãn dữ liệu (mục 9); file `nda` không rời mạng nội bộ.

### 12.6 Dự án private có server và tool riêng (on-prem)

Nhiều công ty chip giữ tool EDA, license, PDK và dữ liệu thiết kế trên **server nội bộ**,
đôi khi **không có internet**. Tool vẫn áp dụng được theo một trong ba cách triển khai:

| Cách | Engine chạy ở | Tool EDA | Model AI | Hợp khi |
|---|---|---|---|---|
| **A. Laptop gọi server** | Máy kỹ sư | Trên server, gọi qua runner `ssh`/`lsf`/`slurm`/`sge` | Cloud (Claude, GLM) | Được dùng AI cloud; chỉ tool và license nằm trên server |
| **B. Cài trên server** | Server Linux của dự án, mỗi kỹ sư chạy CLI trong tài khoản của mình | Chạy local trên server hoặc qua job scheduler | Cloud, qua proxy của công ty | Dữ liệu thiết kế không được rời server, nhưng cho gọi API ra ngoài |
| **C. Hoàn toàn nội bộ (air-gapped)** | Server nội bộ | Local hoặc qua scheduler | **Model tự host**: GLM open-weight chạy trên server GPU (vLLM hoặc tương tự) | Không có internet; dữ liệu NDA tuyệt đối |

**Những gì design đã có sẵn cho trường hợp này:**
- adapter `cmd` để bọc script, Makefile, Tcl riêng của dự án (mục 7.1);
- runner `ssh`/`lsf`/`slurm` cho server có license;
- nhãn dữ liệu và chặn file `nda`;
- quy ước dự án (naming, layout, template) khai báo dạng dữ liệu, nên dự án khác dùng luật
  của họ mà không sửa tool.

**Những gì phải bổ sung** (xem D25–D27):

| Thiếu | Vì sao | Bổ sung |
|---|---|---|
| **Môi trường server** | Server chip thường dùng Environment Modules/Lmod, biến license (`LM_LICENSE_FILE`), đường dẫn tool riêng | Profile có mục `env`: module cần load, biến môi trường, license; `doctor` kiểm hết |
| **Hệ quản lý phiên bản khác git** | Nhiều công ty dùng Perforce, ClioSoft SOS, SVN, hoặc GitLab/Gerrit thay GitHub | **VCS adapter** cho state và artifact; **review adapter** cho gate (GitHub PR, GitLab MR, Gerrit, hoặc file duyệt). Git + GitHub chỉ là adapter mặc định |
| **Model không phải Claude** | Chế độ C dùng model tự host, thường có API kiểu OpenAI, không phải Anthropic | **Runtime adapter thứ hai**: vòng agent tự viết với tool calling qua API tương thích OpenAI, hoặc proxy chuyển đổi. Claude Agent SDK chỉ là runtime mặc định |
| **Cài không có internet** | Không có PyPI, không có GitHub | Gói cài offline: wheelhouse có đủ thư viện, pack, Docker image; hoặc mirror PyPI nội bộ |
| **Nhiều người trên cùng server** | Cùng repo, cùng license | Khóa theo block (đã có); hàng đợi license trong runner; cache theo từng user |
| **Tắt mọi kết nối ra ngoài** | Chính sách bảo mật | Cấu hình `offline: true`: tắt Researcher web, tắt trace ra ngoài, chỉ dùng model nội bộ |

```yaml
# ví dụ .chipgraph.yml của một dự án on-prem (chế độ C)
project: chip_x
extends: [git+https://git.internal/company_x/chip-rules@v3, preset:mcu-soc]   # offline: mirror nội bộ
offline: true
env:
  modules: [vcs/2026.03, verdi/2026.03, dc/2026.03]
  vars: { LM_LICENSE_FILE: "27000@lic01" }
vcs:    { use: perforce, depot: "//chip_x/..." }
review: { use: gerrit, url: "https://gerrit.internal" }
runner: { use: lsf, queue: "rtl_short" }
adapters:
  sim:  { use: cmd, cmd: "make -C $CHIPX_ROOT sim BLOCK={block}", parser: vcs }
  lint: { use: cmd, cmd: "spyglass_run.sh {block}", parser: spyglass }
models:
  providers: { local: { api: openai-compatible, base_url: "http://gpu01:8000/v1" } }
  tiers: { small: local:glm-small, medium: local:glm, large: local:glm }
runtime: generic            # vòng agent tự viết, không dùng Claude Agent SDK
data: { default: nda }
```


---

## 13. Cấu trúc repo

```
chipgraph/
  DESIGN.md · AGENTS.md · README.md · LICENSE · SECURITY.md · CONTRIBUTING.md
  pyproject.toml · uv.lock · renovate.json
  docs/            IMPLEMENTATION_PLAN.md · DECISIONS.md · RESEARCH.md · archive/
  src/chipgraph/   package Python duy nhất cài bằng uv
    core/                  KHÔNG import adapters/ hay packs (kiểm bằng import-linter)
      contracts/           pydantic model: Artifact, Rule, Check, Gate, Event, Profile...
      engine/              graph, scheduler, stale/hash, gate, budget, lock, decide()
      model/               Design Model: schema lõi, store SQLite, query API
      runtime/             giao diện agent runtime, vai, skill loader, sandbox bridge
      state/               journal, approvals, runs, cache
      config/              profile nhiều tầng
      plugin_api/          Protocol + registry (entry points)
    adapters/
      tool/                cmd · edalize · pyslang · peakrdl · symbiyosys
      parser/              log parser: verilator · verible · yosys · vcs · generic
      format/              qsoc-contract · systemrdl · ip-xact · opentitan-hjson
      llm/                 anthropic · anthropic-compatible (GLM) · openai-compatible
      runtime/             claude-agent-sdk · generic
      runner/              local · ssh · lsf · slurm · sge
      vcs/                 git · perforce · svn
      review/              github · gitlab · gerrit · file
    checks/                layout · filelist · generated · naming · hardcode · duplicate ...
    cli/                   typer app
    mcp/                   MCP server
    packs/                 pack đi kèm tool, mỗi pack một thư mục có pack.yml (dữ liệu + skill
                           + rule + code nhỏ); thư mục snake_case, tên pack có gạch (D36)
      spec_core/ · lang_sv/ · digital_rtl/ · dv/ · assist/ · pm/
    interfaces/            apb · ahb · axi4 · axi4-lite · wishbone ...
    presets/               mcu-soc · ip-block · fpga-prototype
    orgs/qnsc/             luật (naming-v1.yml), template, chính sách dữ liệu
  plugin/          Claude Code plugin: commands, skills, hooks, cấu hình MCP
  docker/          chipgraph-eda (OSS CAD Suite + Python 3.14 + CLI)
  examples/tinysoc/  dự án mẫu nhỏ, open-source, dùng cho test đầu-cuối
  evals/           bài thi + chấm (Inspect AI)
  tests/           unit · contract · e2e
```

### 13.1 Quy ước cho chính repo tool

| Mục | Quy ước |
|---|---|
| Code Python | `ruff` (format và lint), `mypy` strict cho `core/`; test bằng `pytest` |
| Tên | Pack, rule, check, vai, skill: `lower_snake_case`, có namespace `<pack>/<name>` (ví dụ `digital-rtl/rtl_module`) |
| Schema | Mọi file dữ liệu (rule, pack, profile, model) có JSON Schema và trường `schema_version` |
| Version | Core, plugin API và mỗi pack theo semver; có changelog |
| Commit, PR | Conventional commits; PR cần CI xanh (test, evals nhanh, luật phụ thuộc) |
| Tài liệu | `DESIGN.md` là thiết kế đích; quyết định mới ghi vào `docs/DECISIONS.md` |

---

## 14. Lộ trình

Mỗi mốc là một **lát dọc dùng được thật** trên QSoC. Danh sách task chi tiết (ID, phụ thuộc, output, tiêu chí nghiệm thu) nằm ở
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

| Mốc | Nội dung | Tiêu chí xong |
|---|---|---|
| **M0 Nền** | Core: artifact/rule/check/gate, graph, hash, journal, resume, CLI, MCP khung, adapter `make`, trace; check `layout`, `filelist`, `generated` | Chạy `check` của QSoC qua engine; ngắt giữa chừng rồi `resume` đúng chỗ; CI chặn core import pack |
| **M1 Hiểu** (không sinh code) | `ingest` dựng Design Model QSoC (contract, MAS, RTL qua pyslang); `baseline`; check chéo, `naming` dựa trên pyslang, `hardcode`, `duplicate`, CDC cấu trúc; finding và waiver; `/audit` (phần deterministic); `/ask` có trích dẫn; `/triage`; `/trace`; `gen:diagram`; `learn`, `try`; template ghi đè từng phần; plugin cục bộ; `agents-md` (tùy chọn) | Check chéo tìm ra lệch thật hoặc xác nhận sạch; team dùng `/ask` hằng ngày |
| **M2 Build block** | Rule `rtl_module` + `tb_module` (cocotb) độc lập, vòng sửa, fan-out worktree, Critic review, gate PR | Một block QSoC thật (TIMER hoặc module SYSDBG) ra PR qua CI không sửa tay |
| **M3 Spec và thay đổi** | `/mas`, Critic cho spec có bằng chứng bắt buộc, chuẩn hóa REQ + mâu thuẫn bằng z3, `/impact`, `/change`, đóng băng, SystemRDL + PeakRDL; **workspace nhiều repo** | Diễn lại "bỏ GPIO3" ra đúng bảng tác động; change liên repo ra PR liên kết đúng thứ tự |
| **M4 Verify sâu và đo** | SVA + SymbiYosys, vòng coverage, evals đầu tiên, `report`, trang tĩnh | Có số liệu chất lượng và chi phí theo thời gian |
| **M5 Tổng quát** | Preset `ip-block` trên Sky130 open-source; format adapter hjson; runner ssh, lsf; mục `env`; VCS và review adapter thứ hai (GitLab); runtime generic cho model tương thích OpenAI; gói cài offline; Researcher; dashboard có thao tác | Dự án loại thứ hai chạy được, không sửa core; một lần chạy thử chế độ C (không internet, model tự host) |
| Sau | signoff-rtl (CDC/RDC), low-power, dft, backend, firmware, optimize, analog; `/intake`, `/arch` đầy đủ | Theo nhu cầu dự án thật (mục 8.5) |

M1 đi trước M2 vì an toàn và có giá trị ngay: model và check chéo không sinh code, và
chính nền đó làm agent sinh code ở M2 chính xác hơn.

---

## 15. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Spec sai làm RTL và test cùng sai | Critic, check chéo, gate spec, người duyệt |
| Agent bịa số liệu | Số liệu chỉ lấy từ Design Model; check spec–RTL |
| Lộ dữ liệu NDA | Nhãn dữ liệu, chặn hoặc chuyển model local, sandbox |
| Chi phí cao | Hạng model, cascade, `decide()`, caching, ngân sách |
| Fan-out ghép sai | F1–F7 |
| Xây quá nhiều trước khi có người dùng | Lát dọc theo mốc; mỗi mốc dùng thật trên QSoC |
| Vô tình gắn QSoC vào core | Luật phụ thuộc có CI; dự án loại thứ hai ở M5 |
| Phụ thuộc một nhà cung cấp model | LLM adapter; engine và model độc lập với model AI |

---

## 16. Giữ design không lỗi thời

AI thay đổi theo tháng. Thiết kế chip và kỹ thuật phần mềm thay đổi theo thập kỷ. Design
tách **phần bền** khỏi **phần mau cũ**, và phần mau cũ phải **thay được mà không đụng
lõi**.

### 16.1 Phần bền và phần mau cũ

| Tầng | Ví dụ | Tốc độ đổi | Cách giữ |
|---|---|---|---|
| **Bền** | Build graph, artifact có hash, Design Model, check deterministic, gate có người duyệt, VCS, truy vết | Chậm (năm, thập kỷ): `make` có từ 1976, sign-off chip luôn cần người chịu trách nhiệm | Nằm trong core; đổi rất ít |
| **Chuẩn mở** | MCP, OpenTelemetry, SystemRDL, IP-XACT, cocotb, Edalize | Trung bình (năm) | Dùng qua adapter; theo version của chuẩn |
| **Mau cũ** | Model AI, prompt, skill, agent SDK, vai, chiến lược điều phối, Jev, tool thương mại của vendor | Nhanh (tháng) | Là dữ liệu hoặc adapter; thay thoải mái |

### 16.2 Cơ chế

| # | Cơ chế | Nghĩa là |
|---|---|---|
| O1 | **Mọi thứ mau cũ nằm sau adapter hoặc là dữ liệu** | Đổi model: sửa profile. Đổi agent SDK: viết adapter runtime mới. Đổi prompt: sửa skill. Core không biết model hay SDK cụ thể nào |
| O2 | **Evals là thước đo khi nâng cấp** | Model mới, SDK mới, prompt mới: chạy evals trước. Điểm không tụt thì đổi |
| O3 | **Giàn giáo gỡ được** | Planner, best-of-N, Critic, cascade tồn tại vì model hiện tại còn yếu. Mỗi cái bật tắt được và **đo bằng evals có và không có nó**. Không còn giúp thì bỏ. Model mạnh lên thì nâng mức tự động (L3 lên L4), giảm lượt thử |
| O4 | **Đặt cược vào chuẩn mở** | MCP, OTel, SystemRDL, git, cocotb đổi chậm và có cộng đồng. Không phụ thuộc định dạng riêng của một hãng |
| O5 | **Code của mình nhỏ** | Càng ít code tự viết, càng ít thứ phải giữ. Dùng lại thư viện (D6) |
| O6 | **Plugin API có version (semver)** | Pack và adapter không vỡ khi core nâng cấp; có giai đoạn deprecate trước khi bỏ |
| O7 | **Tự động cập nhật dependency** | Dependabot hoặc Renovate mở PR nâng version; test và evals chạy trên PR đó |
| O8 | **Rà soát định kỳ** | Mỗi quý: Researcher quét model, SDK, tool EDA, paper mới, rồi lập báo cáo đề xuất; mỗi quyết định trong `DECISIONS.md` có dòng "Xem lại khi" |

### 16.3 Kịch bản tương lai và design phản ứng thế nào

| Nếu | Thì |
|---|---|
| Model mạnh hơn nhiều, tự làm được cả block | Bỏ bớt giàn giáo (O3), nâng mức tự động. Build graph, Design Model, check và gate vẫn cần, vì chip phải kiểm chứng và có người ký trước khi tape-out |
| Hãng EDA ra agent riêng có MCP (Synopsys, Cadence, Siemens) | Bọc agent của hãng thành một **rule** hoặc adapter trong graph. Tool của mình trở thành lớp điều phối và truy vết giữa nhiều agent |
| Claude Code đổi định dạng plugin, hoặc team đổi sang client khác | Plugin chỉ là lớp mỏng; engine và MCP giữ nguyên |
| Có chuẩn mới thay MCP | Viết adapter mới cho lớp xuất ra ngoài (mục 7.2) |
| Có kiểu model mới (như Jev) | Cắm vào sau `decide()` hoặc LLM adapter, qua evals trước |
| Dự án cần ngôn ngữ hoặc PDK mới | Thêm pack hoặc adapter (mục 8) |

---

## 17. Câu hỏi còn mở

1. Chính sách dữ liệu khi dùng GLM qua API của Z.ai, và khi nào cần GLM open-weight tự host (D24).
2. Tool thương mại nào có license, trên server nào (cho runner `ssh`/`lsf`)?
3. Trần chi phí cho CI và evals (phần duy nhất có thể tốn tiền ngoài gói Claude của kỹ sư).
4. Block đầu tiên cho M2: TIMER hay một module con của SYSDBG?
5. Ngoài lead (Nghia), ai cùng build, ai chỉ dùng?
6. Kênh thông báo khi gate chờ (terminal, Slack, email).
7. Mốc đóng băng và người duyệt sau đóng băng.
8. Ai quản lý token hoặc API key dùng cho CI và evals?
