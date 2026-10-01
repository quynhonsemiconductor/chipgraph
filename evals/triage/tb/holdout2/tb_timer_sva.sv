// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage holdout2: an assertion-based test of examples/tinysoc's tiny_timer.
//
// Not part of tinysoc, and only used by the second holdout set (`evals/triage/holdout2.yml`):
// `evals/triage/gen_logs.py --set holdout2` copies it into a temporary copy of the project
// (as `tb/tb_timer_sva.sv`) to produce simulation logs with an injected fault.
//
// The stimulus only programs the timer; concurrent assertions (SVA) check it against
// TINY_TIMER_MAS.md on every clock: COUNT steps by one while enabled and holds while not
// (REQ-TIM-001), `irq` rises the cycle after COUNT equals COMPARE (REQ-TIM-003) and never
// without that cause, and IRQ_CLR drops it (REQ-TIM-004). A failing assertion reports
// with `$error`, which ends the run.
//
// Run: verilator --binary --timing --timescale 1ns/1ps rtl/tiny_timer.sv tb/tb_timer_sva.sv \
//   --top-module tb_timer_sva && obj_dir/Vtb_timer_sva

module tb_timer_sva;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 1:0] addr = 2'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic        irq;

  // Observed through the read port: addr is held at COUNT between writes.
  logic [31:0] count;
  logic        enabled = 1'b0;
  logic [31:0] compare = 32'd0;
  logic        writing;

  assign count   = rdata;
  assign writing = wr_en;

  always #5 clk = ~clk;

  tiny_timer dut (
      .clk  (clk),
      .rst_n(rst_n),
      .addr (addr),
      .wr_en(wr_en),
      .wdata(wdata),
      .rdata(rdata),
      .irq  (irq)
  );

  // REQ-TIM-001: one step per enabled cycle, no step while disabled.
  a_count_step : assert property (@(posedge clk) disable iff (!rst_n)
      (!writing && addr == 2'd0 && !$past(writing)) |->
        (count == $past(count) + (enabled ? 32'd1 : 32'd0)))
  else $error("a_count_step: COUNT went 0x%0h -> 0x%0h with EN=%b", $past(count), count, enabled);

  // REQ-TIM-003: irq is high the cycle after COUNT equals COMPARE while enabled.
  a_irq_after_match : assert property (@(posedge clk) disable iff (!rst_n)
      (enabled && !writing && addr == 2'd0 && count == compare) |=> irq)
  else $error("a_irq_after_match: COUNT reached COMPARE=0x%0h but irq=%b on the next cycle",
              compare, irq);

  // REQ-TIM-003: irq never rises without COUNT == COMPARE on the cycle before.
  a_irq_has_cause : assert property (@(posedge clk) disable iff (!rst_n)
      $rose(irq) |-> $past(enabled && count == compare))
  else $error("a_irq_has_cause: irq rose with COUNT=0x%0h, COMPARE=0x%0h on the cycle before",
              $past(count), compare);

  task automatic write_reg(input logic [1:0] a, input logic [31:0] d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(negedge clk);
    wr_en = 1'b0;
    addr  = 2'd0;
    if (a == 2'd1) compare = d;
    if (a == 2'd2) enabled = d[0];
    $display("tb_timer_sva: [%0t] write 0x%0h = 0x%08h", $time, a, d);
  endtask

  initial begin
    $display("tb_timer_sva: start");
    repeat (2) @(negedge clk);
    rst_n = 1'b1;

    write_reg(2'd0, 32'd0);
    write_reg(2'd1, 32'd6);
    write_reg(2'd2, 32'h1);  // EN = 1
    repeat (12) @(negedge clk);
    if (irq !== 1'b1) $error("tb_timer_sva: irq low 12 cycles after EN=1 with COMPARE=6");
    write_reg(2'd2, 32'h2);  // EN = 0, IRQ_CLR = 1
    repeat (2) @(negedge clk);
    if (irq !== 1'b0) $error("tb_timer_sva: irq still high after IRQ_CLR");
    repeat (4) @(negedge clk);

    $display("tb_timer_sva: PASS");
    $finish;
  end

endmodule
