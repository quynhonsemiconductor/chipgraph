# chipgraph — Research

Các nghiên cứu, tool và cách làm của ngành đã xem xét khi thiết kế. Thiết kế chốt nằm ở
[`../DESIGN.md`](../DESIGN.md); lý do chọn nằm ở [`DECISIONS.md`](DECISIONS.md).

## 1. Các tool và nghiên cứu đã có

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
+ profile nhiều dự án + kho kiến thức tích lũy**. Đó là chỗ của `chipgraph`.

## 2. Các công ty lớn đang làm gì (research 2026-09-27)

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

## 3. Đề xuất đã rút ra

Trạng thái từng đề xuất nằm ở `DECISIONS.md`. Số mục trong bảng dưới theo bản v0.1
(`archive/DESIGN_v0.1.md`); bản v0.2 đã áp dụng I1–I7 và I9, còn I8 để sau.


| # | Ý tưởng | Học từ | Thay đổi trong design | Mức |
|---|---|---|---|---|
| I1 | **Design Model**: nâng spec model thành một mô hình tri thức chung, gồm spec (mục 6) + dữ kiện trích từ RTL (phân cấp, port, clock, reset, thanh ghi, FSM) + quan hệ giữa chúng. Agent hỏi qua tool `model.query`, không đọc file thô | Cadence Mental Model | Mục 6 mở rộng; thêm `chipgraph ingest` để dựng model từ dự án có sẵn (spec + RTL) | **Migrate**: tốt hơn cách cũ |
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
- Nemotron hay model local: chỉ khi chính sách NDA bắt buộc (DESIGN.md mục 9).

---

## 4. Tham khảo

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
- Archify — https://betterstack.com/community/guides/ai/archify-architecture/ · https://taku.ai/blog/skill-archify
- Harness engineering: deepset — https://www.deepset.ai/blog/harness-engineering · O'Reilly — https://www.oreilly.com/radar/agent-harness-engineering/ · Model or Harness? — https://arxiv.org/pdf/2607.28802 · Skill-mediated agents — https://arxiv.org/pdf/2606.20631 · State of harness engineering 2026 — https://marmelab.com/blog/2026/09/24/the-state-of-ai-harness-engineering-2026.html
- AGENTS.md — https://codersera.com/blog/agents-md-complete-guide-2026/ · tác động lên hiệu quả agent — https://arxiv.org/html/2601.20404v2
- GitHub Spec Kit — https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/
- Z.ai: Claude Code với GLM (endpoint tương thích Anthropic) — https://docs.z.ai/scenario-example/develop-tools/claude · GLM-5.2 — https://www.datacamp.com/blog/glm-5-2 · Z.ai 2026 review — https://buttondown.com/aiexpose/archive/zai-review-2026-i-tested-it-glm-5-features-and/
