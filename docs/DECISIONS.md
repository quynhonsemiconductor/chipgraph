# chipgraph — Decisions

Lý do cho từng lựa chọn trong [`../DESIGN.md`](../DESIGN.md) v0.2. Mỗi quyết định ghi
phương án đã cân nhắc và vì sao chọn. Research gốc nằm ở [`RESEARCH.md`](RESEARCH.md).

| Trạng thái | Nghĩa |
|---|---|
| **Đề xuất** | Architect đề xuất, chờ chủ dự án chốt |

**2026-09-27:** lead chốt toàn bộ quyết định đang ở trạng thái "Đề xuất" theo đúng đề xuất.
| **Chốt** | Đã đồng ý |
| **Bỏ** | Đã cân nhắc và không làm |

---

## D1. Sản phẩm trên lõi nhỏ, không phải framework — Chốt (2026-09-27)

- **Phương án:** (a) framework agent đa dụng; (b) bộ skill/prompt thuần; (c) sản phẩm có
  lệnh dùng ngay, xây trên lõi nhỏ có điểm mở rộng.
- **Chọn (c).** Giá trị nằm ở quy trình chip, luật và check, không nằm ở API tổng quát.
  (b) không có state, resume, gate cứng, không chạy được trong CI. (a) tốn công giữ API
  khi chưa có người dùng.

## D2. Engine tất định bên ngoài, không để LLM điều phối — Chốt (2026-09-27)

- **Phương án:** (a) một agent chính đọc hướng dẫn rồi tự điều phối (kiểu flow slash
  command); (b) engine bằng code quyết định bước tiếp, agent chỉ làm việc nhỏ.
- **Chọn (b).**
  - Hệ nhiều agent hỏng chủ yếu vì giao việc mơ hồ, lệch nhau và không kiểm chứng (MAST).
  - Anthropic khuyên dùng workflow cố định trước.
  - Engine bằng code resume được, chạy được trong CI và tái lập được.
- Claude Code vẫn là giao diện chính: nó gọi engine qua MCP và CLI.

## D3. Build graph là lõi — Chốt (2026-09-27)

- **Phương án:** (a) máy trạng thái theo stage (spec → plan → rtl → review); (b) build
  graph artifact–rule như `make`/Bazel, trong đó agent là một loại rule.
- **Chọn (b).** Một cơ chế (hash và stale) cho ra cùng lúc resume, quản lý thay đổi,
  fan-out, truy vết và gate. Người sửa tay hay AI sửa đều được graph nhận biết như nhau.
- Workflow theo stage vẫn tồn tại, nhưng chỉ là **tập hợp rule** (dữ liệu), không phải
  một cơ chế riêng.
- Khác v0.1: v0.1 coi máy trạng thái là lõi, còn stale tracking là tính năng phụ.

## D4. Design Model thay cho "spec model" — Chốt (2026-09-27)

- **Nguồn:** Cadence ChipStack "Mental Model" (RESEARCH I1).
- **Phương án:** (a) agent đọc file spec và RTL thô; (b) một mô hình có kiểu, dựng từ
  spec máy đọc, spec chữ và phân tích tĩnh RTL, rồi agent truy vấn qua tool.
- **Chọn (b).**
  - Chống bịa số liệu, vì agent tra fact chứ không tự suy.
  - Check chéo deterministic chạy được ngay trên model.
  - `ingest` giúp dự án có sẵn dùng được mà không phải viết lại spec.
- Model được lưu ở file trong git và một index SQLite sinh lại được, xem D5.

## D5. Git là sự thật; cache sinh lại được — Chốt (2026-09-27) (đã sửa bởi D26, D31)

- **Phương án:** (a) database trung tâm (server); (b) git cho artifact, duyệt và tóm tắt
  run, SQLite làm cache cục bộ, trace chi tiết đưa lên backend OTel.
- **Chọn (b).**
  - Team đã làm việc qua git và PR.
  - Không cần server để bắt đầu; audit có sẵn.
  - Dashboard sau này chỉ đọc state từ git.

## D6. Dùng lại Edalize, pyslang, PeakRDL, cocotb, SymbiYosys — Chốt (2026-09-27)

- **Phương án:** tự viết adapter, parser và generator; hay dùng lại thư viện open-source.
- **Chọn dùng lại:**
  - Edalize đã bọc nhiều simulator, synth và FPGA.
  - pyslang là parser SystemVerilog đầy đủ.
  - PeakRDL sinh RTL thanh ghi, header và tài liệu từ SystemRDL (chuẩn Accellera).
  - cocotb là testbench Python, LLM viết tốt, chạy trên nhiều simulator.
- Dự án có flow riêng vẫn dùng được qua adapter `make`.

## D7. Gate nặng là PR, gate nhẹ ghi trong journal — Chốt (2026-09-27) (chi tiết lưu trữ: D31)

- **Gate nặng** (spec, RTL): duyệt bằng PR như team đang làm, nên audit và review có sẵn.
- **Gate nhẹ** (plan, trả lời câu hỏi): duyệt ngay trong phiên, ghi vào
  `.chipgraph/decisions/<id>.yml` kèm hash (D31).
- **Lý do:** không đẻ thêm quy trình mới; không tạo quá nhiều PR.

## D8. Ít vai, vai là dữ liệu — Chốt (2026-09-27)

- **Phương án:** (a) 9 agent riêng như v0.1 (Coder, Tester, Debugger, Spec Writer…); (b)
  5 vai (Author, Critic, Planner, Researcher, Triage), khả năng theo loại artifact nằm ở
  skill.
- **Chọn (b).**
  - Ít prompt hệ thống phải giữ.
  - Thêm loại artifact mới là thêm skill, không thêm agent.
  - Tính độc lập giữa người viết và người kiểm vẫn giữ, vì đó là hai **rule** khác nhau
    với input khác nhau, không phải vì là hai agent khác nhau.

## D9. Tự viết engine, không dùng LangGraph hay Temporal lúc đầu — Chốt (2026-09-27)

- **Lý do:**
  - Engine cần nhỏ: graph, hash, journal, scheduler.
  - LangGraph mạnh về agent graph, nhưng không có sẵn khái niệm artifact có hash hay stale.
  - Temporal quá nặng cho team nhỏ chạy trên máy cá nhân.
- **Xem lại khi:** cần chạy dài trên server cho nhiều người.

## D10. Claude Agent SDK làm agent runtime, có LLM adapter — Chốt (2026-09-27), **sửa bởi D35**: Agent SDK chỉ là runtime API, không còn là mặc định

- **Lý do:**
  - Có subagent, giới hạn tool theo agent, MCP; team đang dùng Claude.
  - LLM adapter giữ đường lui cho model khác hoặc model local (NDA).

## D11. Sandbox dùng lại, không tự viết — Chốt (2026-09-27)

- **Nguồn:** NVIDIA OpenShell (RESEARCH I4).
- **Chọn:** OpenShell nếu chạy tốt trên máy của team; nếu không thì container có chính
  sách ghi và mạng. Cả hai đều cho audit trail.

## D12. Mốc "Hiểu" (M1) đi trước mốc sinh code (M2) — Chốt (2026-09-27)

- **Nguồn:** NVIDIA ChipNeMo (chatbot kỹ sư, triage bug), Synopsys Copilot (RESEARCH I5).
- **Lý do:**
  - `ingest`, check chéo, `/ask`, `/triage` không sinh code nên an toàn, và team dùng
    được hằng ngày.
  - Design Model là nền để M2 sinh code chính xác hơn.

## D13. Kiểm từng bước — Chốt (2026-09-27)

- **Nguồn:** Siemens self-verifying (RESEARCH I2).
- **Chọn:** mỗi lần agent ghi file thì check chạy ngay, không đợi cuối task. Lỗi được
  bắt sớm, và vòng sửa ngắn hơn.

## D14. Lớp quyết định nhanh `decide()` — Chốt (2026-09-27)

- **Nguồn:** Jev, REFLEX.
- **Chọn:** luật bằng code, rồi model nhỏ có structured output, rồi model mạnh.
- Jev chỉ cắm vào sau khi qua evals và chính sách dữ liệu. Nó mới ra (2026-09-15), là sản
  phẩm đóng, và dữ liệu phải gửi lên cloud.
- **Bổ sung (2026-10-01), sau lần chạy holdout của triage (PR #65):** khi câu hỏi đã leo
  lên hạng sau, câu trả lời hợp lệ của hạng đó là kết quả, kể cả khi dưới ngưỡng của chính
  nó (khi đó đánh dấu `low_confidence`). Chỉ quay về câu trả lời của hạng trước khi hạng
  sau không chọn được đáp án nào trong các lựa chọn. Không bao giờ so độ tin cậy giữa các
  hạng: mỗi model tự báo độ tin cậy theo cách riêng, và model nhỏ thường tự tin quá mức
  (holdout: Haiku trả `rtl` 0.70, Opus trả `tb` 0.45 là đúng, nhưng luật cũ chọn câu có độ
  tin cậy cao hơn là `rtl`).

## D15. Chống lỗi thời bằng tách tầng, evals và rà soát định kỳ — Chốt (2026-09-27)

- **Chọn:** phần bền (build graph, Design Model, check, gate, git) nằm trong core; phần
  mau cũ (model, prompt, SDK, chiến lược điều phối) là dữ liệu hoặc adapter; giàn giáo
  quanh model đo bằng evals và gỡ được (DESIGN mục 16).
- **Xem lại khi:** mỗi quý, hoặc khi có model hay SDK thế hệ mới.

## D16. Hình vẽ sinh deterministic từ Design Model — Chốt (2026-09-27)

- **Nguồn:** Archify: agent sinh IR JSON có kiểu, đối chiếu với code, rồi render
  deterministic.
- **Chọn:** áp dụng **cách làm**, không dùng chính tool. Archify nhắm vào kiến trúc phần
  mềm, còn Design Model của mình đã là IR có kiểu cho chip. Có thể dùng Archify để vẽ kiến
  trúc code của chính `chipgraph`.

## D17. Phân loại lỗi harness: context / constraint / verification / planning / infra — Chốt (2026-09-27)

- **Nguồn:** deepset harness engineering; "Model or Harness?" taxonomy.
- **Chọn:** mọi fail đều được gắn nhãn, và nhãn chỉ ra thành phần harness phải sửa. Harness
  được cải tiến theo lịch sử lỗi thật, không theo cảm giác.

## D18. Sinh `AGENTS.md` cho dự án từ profile — Chốt (2026-09-27) (thành tùy chọn theo D31)

- **Nguồn:** chuẩn AGENTS.md (Agentic AI Foundation).
- **Chọn:** `chipgraph init` sinh `AGENTS.md` và `CLAUDE.md` từ profile và luật tổ chức. Agent
  ngoài tool này cũng theo cùng luật, và chỉ có một nguồn.

## D19. Spec-driven development: xác nhận hướng, không dùng Spec Kit — Chốt (2026-09-27)

- **Nguồn:** GitHub Spec Kit (specify → plan → tasks → implement).
- **Chọn:** hướng của mình cùng tinh thần (spec trước, plan, task, làm), nhưng có thêm
  Design Model, check EDA và artifact đặc thù chip. Spec Kit nhắm vào phần mềm chung, nên
  không dùng làm nền. Học cách đặt "constitution", tức luật của tổ chức, thành file.

## D20. Naming, layout, filelist là dữ liệu và là check bắt buộc — Chốt (2026-09-27)

- **Chọn:**
  - Quy ước của dự án (naming, mẫu đường dẫn, header, file sinh, vendor) khai báo trong
    profile và được kiểm như mọi check khác.
  - Naming check dùng pyslang để biết loại đối tượng, chính xác hơn regex trên text.
  - `outputs` của rule sinh từ mẫu `layout`, nên agent không tự đặt đường dẫn.
- **Lý do:** agent sinh file nhanh; không có luật cứng thì repo loạn. QSoC đã chứng minh
  giá trị của `naming_check`, `hardcode_check`, `wrap-check`.

## D21. Python cho toàn bộ tool — Chốt (2026-09-27)

| Tiêu chí | Python | Go | Rust |
|---|---|---|---|
| Thư viện EDA (cocotb, Edalize, pyslang, PeakRDL, FuseSoC, OpenROAD/KLayout API) | **Có hết** | Gần như không | Gần như không |
| Agent SDK chính thức (Claude Agent SDK) | **Có** (Python, TypeScript) | Không | Không |
| MCP SDK, OpenTelemetry, Inspect AI, Langfuse | **Có** | MCP, OTel có; evals không | MCP, OTel có; evals không |
| Kỹ sư chip đọc và sửa được (viết pack, adapter) | **Quen** (cùng với Tcl) | Ít người biết | Ít người biết |
| Hiệu năng | Đủ: tải là chờ LLM và chờ EDA chạy (phút, giờ), engine gần như không tốn CPU | Nhanh hơn, nhưng không phải chỗ nghẽn | Nhanh nhất, nhưng không phải chỗ nghẽn |
| Phát hành | `uv tool install` một lệnh | Một file binary | Một file binary |
| An toàn kiểu | mypy strict + pydantic | Tốt | Tốt nhất |

- **Chọn Python 3.14** (hỗ trợ từ 3.13) với `uv`, `pydantic` cho schema, `asyncio` cho fan-out, `typer`
  cho CLI, `mypy --strict` cho `core/`.
- **Bỏ phương án lai** (core bằng Go hoặc Rust, pack bằng Python): phải giữ hai ngôn ngữ và
  một lớp giao tiếp giữa chúng, và người đóng góp bị chia ra, mà không được lợi gì vì hiệu
  năng không phải chỗ nghẽn.
- **Xem lại khi:** đo được một đoạn nóng thật (ví dụ hash hay parse repo rất lớn). Khi đó
  chỉ viết đoạn đó bằng Rust và gọi qua PyO3.

## D22. Tên chính thức: `chipgraph` — Chốt (2026-09-27)

- **Chọn `chipgraph`:** nói đúng ý tưởng lõi (build graph cho chip), ngắn, trống trên PyPI
  và GitHub khi kiểm ngày 2026-09-27.
- **Tên dẫn xuất:** CLI `chipgraph`, profile `.chipgraph.yml`, thư mục state `.chipgraph/`,
  package Python `chipgraph`, Docker image `chipgraph-eda`, plugin Claude Code `chipgraph`.
- **Việc còn lại trước khi public:** kiểm trademark và tên miền; giữ tên trên PyPI bằng một
  bản `0.0.1` rỗng.

| Tên đã cân nhắc | PyPI | GitHub | Ghi chú |
|---|---|---|---|
| `icagent` | Trống | Có vài repo nhỏ trùng tên | Chung chung, khó tìm kiếm |
| **`chipgraph`** | Trống | Không trùng | **Chọn** |
| `siliconwright` | Trống | Không trùng | Dài hơn |
| `siliconloom` | Trống | Không trùng | Ít nói về chức năng |

## D23. License Apache-2.0 — Chốt (2026-09-27)

- **Lý do:**
  - Cho dùng thương mại và có điều khoản cấp quyền sáng chế (patent grant), quan trọng
    trong ngành chip.
  - Tương thích để gọi hoặc import các thư viện đang dùng: cocotb, Edalize (BSD), pyslang
    (MIT), PeakRDL (LGPL, dùng như thư viện hay tool).
- **Phương án:** MIT (đơn giản hơn nhưng không có patent grant); GPL (hạn chế người dùng
  doanh nghiệp).

## D24. Nhiều nhà cung cấp model: Claude và GLM — Chốt (2026-09-27)

- **Bối cảnh:** công ty có Claude và GLM (Z.ai).
- **Chọn:**
  - LLM adapter hỗ trợ cả hai. Theo tài liệu Z.ai, GLM có endpoint tương thích Anthropic,
    và Claude Code chạy được với nó; cần kiểm ở M0 là Claude Agent SDK cũng chạy được.
  - Hạng model mặc định vẫn là Claude. GLM là **phương án thay thế** cho từng vai, chọn
    theo evals: có thể rẻ hơn ở hạng vừa.
  - **GLM có bản open-weight** (GLM-5.1 theo license MIT). Đây là ứng viên chính cho
    **model tự host khi có dữ liệu NDA**.
- **Cần chốt:** chính sách dữ liệu khi gửi qua API của Z.ai.

## D25. Ba cách triển khai: laptop gọi server, cài trên server, air-gapped — Chốt (2026-09-27)

- **Bối cảnh:** dự án private thường giữ tool, license, PDK, dữ liệu trên server nội bộ.
- **Chọn:** cùng một CLI chạy được ở cả ba cách (DESIGN mục 12.6); khác nhau chỉ ở profile
  (`env`, `runner`, `models`, `offline`).

## D26. VCS và review là adapter; git + GitHub chỉ là mặc định — Chốt (2026-09-27)

- **Lý do:** ngành chip hay dùng Perforce, ClioSoft SOS, SVN, GitLab, Gerrit. Nếu core gắn
  cứng vào git và GitHub thì không áp dụng được cho các dự án đó.
- **Hệ quả:** D5 ("git là sự thật") đổi thành "**VCS của dự án** là sự thật". Hash, journal
  và duyệt vẫn giữ nguyên, chỉ cách lưu thay đổi.

## D27. Runtime agent thứ hai cho model tự host — Chốt (2026-09-27)

- **Lý do:** chế độ air-gapped dùng model tự host (ví dụ GLM open-weight) qua API tương
  thích OpenAI. Claude Agent SDK không phục vụ trường hợp này.
- **Chọn:** runtime adapter có hai bản: `claude-agent-sdk` (mặc định) và `generic` (vòng
  agent tự viết: tool calling, MCP client, giới hạn quyền). Evals chạy trên cả hai để biết
  chất lượng khi dùng model nội bộ.

## D28. Phát hiện vấn đề theo 5 lớp, bằng chứng bắt buộc, AI không chặn build — Chốt (2026-09-27)

- **Chọn:**
  - 5 lớp (DESIGN 4.8): cấu trúc và số liệu; mâu thuẫn trong spec; RTL sai spec; thiết kế vô
    lý; lệch quy trình.
  - Chỉ check deterministic (check chéo, lint, synth, formal, graph) mới được chặn build.
  - Phát hiện của AI chỉ là `warning` hoặc `question`, và **bắt buộc có bằng chứng**.
  - Waiver gắn với hash.
- **Lý do:** giữ độ tin cậy. Nếu AI báo nhầm mà chặn được build thì team sẽ bỏ qua mọi cảnh
  báo; còn phát hiện có bằng chứng thì người kiểm lại nhanh.
- Ràng buộc số trong spec giải bằng z3 thay vì để LLM tự suy.

## D29. Quy ước thuộc về dự án; người dùng chỉ chỉnh trải nghiệm — Chốt (2026-09-27)

- **Chọn:**
  - Quy ước artifact (layout, naming, template, check) do tổ chức và dự án quyết; có ngoại lệ
    theo đường dẫn.
  - Tầng người dùng chỉ chỉnh trải nghiệm cá nhân (mức tự động trong giới hạn, thông báo,
    ngôn ngữ, model được phép).
  - Linh hoạt nhờ: `learn` (học từ repo có sẵn), template ghi đè từng phần, code mẫu của dự án,
    ngoại lệ theo đường dẫn, plugin cục bộ, luật chia sẻ qua git.
- **Lý do:** nếu mỗi người một quy ước thì repo của team loạn. Nhưng mỗi dự án phải giữ được
  cách làm của mình, không bị tool áp khuôn.

## D30. Workspace cho dự án nhiều repo; dùng lại cách ghép repo sẵn có — Chốt (2026-09-27)

- **Chọn:**
  - Workspace khai báo trong repo hub, ref của từng repo được khóa.
  - Có adapter cho git submodule, Google `repo`, Zephyr `west`, FuseSoC, Bender, manifest
    vendor.
  - Design Model và stale đi xuyên qua các repo.
  - Một change liên repo sinh nhiều PR có liên kết.
  - `ArtifactRef` có trường `repo` ngay từ M0.
- **Lý do:** dự án chip thật thường có nhiều repo và nhiều nhóm sở hữu. Ép về monorepo là không
  thực tế. Các công cụ ghép repo đã có sẵn và team đang dùng, nên tool đọc chúng thay vì thay
  thế chúng.

## D31. Dấu vết tối thiểu trong repo dự án — Chốt (2026-09-27) (sửa D5, D7, D18)

- **Vấn đề của bản trước:**
  - Journal, run và `HANDOFF.md` được commit trên branch run: làm repo nặng, gây nhiễu, có
    thể lộ prompt.
  - `AGENTS.md` được sinh mặc định: có thể ghi đè file của team.
  - Manifest workspace bắt buộc nằm ở repo hub.
- **Chọn:**
  - Chỉ sản phẩm thiết kế, cấu hình và quyết định cần audit mới vào repo.
  - State chạy máy nằm ở state backend (mặc định cục bộ, bị gitignore; tùy chọn branch mồ côi
    hoặc repo riêng).
  - Gate nặng dùng chính review của PR.
  - `AGENTS.md` là tùy chọn và chỉ quản lý một đoạn có đánh dấu.
  - Thử không dấu vết bằng `chipgraph try` (D33); cờ `readonly` cho repo không được sửa.
  - Quyết định cần audit: **mỗi quyết định một file** trong `.chipgraph/decisions/`, để hai
    người duyệt cùng lúc không bị conflict (sửa sau review ngày 2026-09-27).
- **Tiền lệ:** Terraform tách state ra backend thay vì commit; pre-commit, editorconfig chỉ
  thêm một file cấu hình nhỏ; Google `repo` dùng một repo manifest riêng.

## D32. Profile mặc định nằm ngoài repo dự án — Bỏ (thay bằng D33)

- **Chọn:**
  - Không cần file nào trong repo dự án. Profile được tìm theo thứ tự: repo cấu hình của
    tổ chức (ánh xạ repo → profile, giống repo `.github` của org hay preset của Renovate),
    repo manifest, `.chipgraph.yml` (tùy chọn, team tự quyết), rồi `--profile`.
  - Run manifest ghi commit của profile để giữ tái lập.
  - Không có profile thì chỉ cho lệnh chỉ đọc.
- **Đánh đổi chấp nhận:**
  - Đổi luật và code thành hai PR ở hai repo.
  - CI của repo dự án cần biết repo cấu hình ở đâu, hoặc chạy CI tập trung.
  - Team muốn luật đi cùng code thì tự thêm `.chipgraph.yml`.

## D33. Một cách cấu hình duy nhất: `.chipgraph.yml` trong repo — Chốt (2026-09-27) (thay D32)

- **Vấn đề của D32:** nhiều chỗ đặt profile cộng với tự tra theo URL làm người dùng rối
  ("cấu hình này từ đâu ra?").
- **Chọn:**
  - Mỗi repo có một `.chipgraph.yml`. Luật chung dùng `extends` từ git có khóa version.
  - Thử tool dùng `chipgraph try` (chỉ đọc, không ghi file).
  - `--profile` chỉ là lối thoát nâng cao cho repo không được sửa.
- **Lý do:** một khái niệm, giống các tool dev quen thuộc. Luật đi cùng code; nhiều repo vẫn
  dùng chung luật qua `extends`.
- **Giữ từ D31:** state chạy máy không vào repo; gate nặng dùng review của PR.

## D34. Baseline cho dự án đang chạy — Chốt (2026-09-27)

- **Vấn đề:** dự án có sẵn chưa có quyết định nào trong chipgraph, nên lệnh build đầu tiên
  dừng ở mọi gate.
- **Chọn:** `chipgraph baseline` đề xuất coi artifact trên nhánh chính là đã duyệt tại hash
  hiện tại (dựa trên lịch sử merge qua PR). Lead xác nhận một lần. Finding có sẵn được ghi
  nhận, không chặn build.

## D35. Runtime mặc định: Claude Code của chính người dùng — Chốt (2026-09-28)

- **Vấn đề:** bản trước để engine tự gọi model bằng API key của tổ chức, kể cả khi kỹ sư đang
  dùng Claude Code. Như vậy mỗi người phải có key, và tốn thêm tiền ngoài gói Claude đã có.
- **Chọn:**
  - Runtime `claude-code`: engine giao task qua MCP, Claude Code của người dùng chạy
    subagent theo vai, hook của plugin giới hạn quyền ghi, `submit` kiểm kết quả.
  - Chi phí nằm trong gói Claude của người dùng. chipgraph không giữ credential và không
    làm đăng nhập claude.ai.
  - Runtime API (`claude-agent-sdk`, `generic`) chỉ cho CI, evals, GLM qua API, và model tự
    host.
- **Lý do:**
  - Team đã có gói Claude.
  - Theo tài liệu Agent SDK, bên thứ ba không được đưa đăng nhập claude.ai vào sản phẩm riêng
    khi chưa được duyệt. Chạy trong Claude Code thì dùng gói hợp lệ.
- **Rủi ro:** chính sách của Anthropic cho chế độ headless (`claude -p`, GitHub Action) đang
  thay đổi trong 2026. Adapter tách riêng, nên chỉ cần đổi cấu hình khi chính sách đổi.
- **Xem lại khi:** Anthropic đổi điều khoản; hoặc spike S7 cho thấy Claude Code không giới
  hạn quyền subagent đủ chặt.

## D36. Pack và dữ liệu đi kèm tool nằm trong package Python — Chốt (2026-09-29)

- **Vấn đề:** plan để pack ở `packs/<tên>/` tại gốc repo, còn luật tổ chức và preset ở `orgs/`,
  `presets/`. Wheel chỉ đóng gói `src/chipgraph`, nên cài bằng `uvx chipgraph` thì không có pack
  nào. Tên có dấu gạch (`spec-core`) cũng không import được, trong khi code của pack (extractor,
  generator) phải được tìm qua entry point.
- **Chọn:**
  - Pack đi kèm tool nằm ở `src/chipgraph/packs/<tên_snake>/`: `pack.yml`, `__init__.py` và code.
    Tên trong `pack.yml` vẫn là tên có gạch (`spec-core`); chỉ tên thư mục là snake_case.
  - Code của pack đăng ký qua entry point `chipgraph.adapters.<loại>` trong `pyproject.toml`,
    như adapter. Task thêm code pack được phép thêm dòng entry point của mình.
  - Luật tổ chức, preset và thư viện interface đi kèm tool nằm ở `src/chipgraph/orgs/`,
    `src/chipgraph/presets/`, `src/chipgraph/interfaces/` (dữ liệu, không phải code).
  - Tìm qua `importlib.resources`, không dò thư mục cha của file nguồn.
  - Thứ tự tìm pack: `.chipgraph/packs/` của dự án → pack đi kèm tool → `$CHIPGRAPH_PACK_PATH`.
    Hai pack trùng tên vẫn là lỗi.
  - `import-linter` cấm `chipgraph.core` import `chipgraph.packs`.
- **Lý do:** một nguồn cho cả khi chạy từ repo và khi cài từ wheel; pack có code được test,
  typecheck và đóng gói như phần còn lại.
- **Bỏ:** giữ `packs/` ở gốc rồi `force-include` vào wheel. Đường dẫn khi dev và khi cài khác
  nhau, và code trong thư mục có gạch vẫn không import được.

## D37. REQ-ID: khai trong spec, hoặc suy ra tạm thời — Chốt (2026-09-29)

- **Vấn đề:** truy vết (`trace`, `/trace`) cần ID cho từng yêu cầu. QSoC đã có ID nhưng không theo
  dạng `REQ-`: 7/13 MAS (DMA, I2C, ROM, SYSCSR, UART, WDT, và BOOT_SPEC) đánh ID kiểu `DMA_001` cho
  từng mục ở phần Verification. 6 MAS còn lại (TIMER, PWM, RAM, SCRC, SYSDBG, Interrupt_Map)
  chưa có ID. Dạng ID khác nhau giữa các dự án, nên không được cố định trong tool.
  (Bản đầu của D37 ghi "QSoC chưa có REQ-ID nào"; câu đó sai, vì chỉ tìm chuỗi `REQ-`.)
- **Chọn:** hai chế độ, cấu hình trong profile (`spec.requirements`).
  - **Khai (mặc định):** REQ-ID viết trong spec, khớp một regex cấu hình được
    (`id_pattern`, mặc định `REQ-[A-Z][A-Z0-9_]*-\d+`; `{block}` và `{BLOCK}` được thay bằng tên
    block). Key là `requirement:<ID>`.
  - **Suy ra (tạm):** `infer: verification`. Mỗi mục đánh số trong mục có tiêu đề
    "Verification" của MAS là một yêu cầu. Key là `requirement:<block>.h<8 hex>`, lấy từ sha256
    của câu đã chuẩn hóa (bỏ số thứ tự, gộp khoảng trắng), nên đánh số lại không làm đổi key.
    Entity ghi `attrs.id_source = "inferred"` (chế độ khai ghi `"declared"`). Finding dựa trên
    yêu cầu suy ra phải nói rõ đó là ID suy ra, không phải ID thật.
- **Lý do:** chạy được trên QSoC ngay: MAS có ID dùng `id_pattern: "{BLOCK}_\d{3}"`, MAS chưa có ID
  dùng chế độ suy ra, trong cùng một profile. Key không lệch khi sửa thứ tự.
- **Rủi ro:** sửa câu chữ của một mục thì key đổi; truy vết cũ của mục đó mất. Chấp nhận, vì đây
  là chế độ tạm.
- **Việc riêng, ngoài chipgraph:** team QSoC ghi quy ước `<BLOCK>_NNN` vào template MAS và thêm ID
  cho 6 MAS còn thiếu; lead bàn với team. Làm xong thì tắt `infer`.
- **Chi tiết:** quy tắc ID, `requirement.missing_id` và các check liên quan ở
  [`REQUIREMENT_IDS.md`](REQUIREMENT_IDS.md).
- **Bổ sung (2026-10-09):** SCRC đã có ID, dạng `SCRC_<AREA>_NNN` (`SCRC_CLK_001`), nên chỉ còn 5 MAS
  chưa có ID (TIMER, PWM, RAM, SYSDBG, Interrupt_Map). Profile mẫu của QSoC khai pattern riêng cho
  `scrc`; trên QSoC hiện có 84 REQ khai và 54 REQ suy ra.

## D38. Block là IP, instance nối vào IP bằng quan hệ `instance_of` — Chốt (2026-09-30)

- **Vấn đề:** hai nguồn gọi block theo hai cách. Profile và thư mục thiết kế dùng tên IP
  (`timer`, `uart`, `gpio`), nên MAS và RTL gắn vào `block:timer`. Contract chip dùng tên
  instance trên memory map (`timer_0`, `timer_1`, `uart_0`, `dma_cfg`, `isram_dbg`). Kết quả là
  `block:timer` và `block:dma` được tham chiếu nhưng không có trong contract, và check chéo
  không biết `timer_0` là một bản của `timer`.
- **Chọn:**
  - Mỗi block trong profile là một **IP**. Profile khai rõ instance của nó:
    `blocks.timer.instances: [timer_0, timer_1]`.
  - Không khai `instances` thì block chỉ khớp instance **cùng tên** trong contract (`pwm` với
    `pwm`). Không đoán bằng cách cắt đuôi `_0` hay tìm tiền tố: có những cặp như `dma` và
    `dma_cfg`, `isram` và `isram_dbg`, mà tên không nói được quan hệ.
  - Thêm kiểu quan hệ lõi `instance_of`: `block:<instance>` → `block:<ip>`. `ingest` tạo block
    IP (nếu contract chưa có) và các quan hệ này.
  - Chỗ chưa map được thì báo, không im lặng: instance trong contract không thuộc IP nào; IP
    khai một instance không có trong contract; entity tham chiếu một block không tồn tại.
    `ingest` báo thành warning; check chéo (M1-07) biến chúng thành finding.
  - Interrupt mà contract đặt tên theo peripheral, không theo block (`dma`, `spi_host`,
    `wdt_wakeup`), thuộc IP có tên đó, hoặc IP khai tên đó trong `instances`
    (`spi: [spi, spi_device, spi_host]`). `ingest` gán block và ghi
    `attrs.block_from = profile`; tên đó không bị báo là instance lạ. (Bổ sung 2026-09-30.)
- **Lý do:** tên không đủ tin để suy ra quan hệ; khai một lần trong profile là rõ và kiểm được.
  MAS mô tả IP, contract mô tả instance, nên hai tầng cần một cạnh nối rõ ràng.
- **Bỏ:** đoán theo tiền tố hoặc hậu tố tên; đổi tên block trong profile cho khớp contract (mất
  liên hệ với thư mục thiết kế và filelist).

## Lịch xem lại

| Quyết định | Xem lại khi |
|---|---|
| D9 (tự viết engine) | Cần chạy dài trên server cho nhiều người |
| D10 (Claude Agent SDK) | Có SDK tốt hơn, hoặc cần model khác vì NDA (đã có D27) |
| D35 (runtime Claude Code) | Anthropic đổi điều khoản; kết quả spike S7 |
| D21 (Python) | Đo được đoạn nóng thật về hiệu năng |
| D24 (Claude + GLM) | Mỗi khi có model mới; theo evals |
| D11 (sandbox) | OpenShell không chạy tốt trên máy team |
| D14 (Jev) | Có evals (M4) và chính sách dữ liệu |
| D8 (5 vai) | Evals cho thấy một vai thừa hoặc thiếu |
| X1 (không fine-tune) | Bắt buộc chạy local và có đủ dữ liệu |
| D37 (REQ-ID suy ra) | Cả 13 MAS của QSoC có ID `<BLOCK>_NNN`; khi đó tắt `infer` |

---

## Đã bỏ

| # | Phương án | Vì sao bỏ |
|---|---|---|
| X1 | Tự train hoặc fine-tune model chip (kiểu ChipNeMo) | Tốn công và dữ liệu; model frontier đã đủ mạnh. Xem lại nếu bắt buộc chạy local |
| X2 | Dựa vào agent thương mại (Synopsys, Cadence, Siemens) | Đóng, có license; chỉ học ý tưởng |
| X3 | Xây trên LangChain hoặc CrewAI | Nặng, dễ bị khóa, khó hiểu hết code |
| X4 | Tự viết sandbox | Đã có OpenShell và container |
| X5 | Từ prompt ra GDS một mạch (kiểu CoreSmith) | Không hợp SoC có nhiều IP, bus, top |
| X6 | Dùng lại file hay prompt của flow thầy | Tool phải độc lập; chỉ học ý tưởng |
