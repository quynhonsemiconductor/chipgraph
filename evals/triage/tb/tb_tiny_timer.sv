// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage: a self-checking testbench for examples/tinysoc's tiny_timer.
//
// Not part of tinysoc: `evals/triage/gen_logs.py` copies it into a temporary copy of the
// project (as `tb/tb_tiny_timer.sv`) to produce simulation logs, with or without an
// injected fault. Checks follow TINY_TIMER_MAS.md: REQ-TIM-001..005.
//
// Run: verilator --binary --timing --timescale 1ns/1ps rtl/tiny_timer.sv tb/tb_tiny_timer.sv \
//   --top-module tb_tiny_timer && obj_dir/Vtb_tiny_timer

module tb_tiny_timer;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 1:0] addr = 2'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic        irq;

  int          errors = 0;
  logic [31:0] value;

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

  task automatic write_reg(input logic [1:0] a, input logic [31:0] d);
    $display("tb: write addr=0x%0h data=0x%0h", a, d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(negedge clk);
    wr_en = 1'b0;
  endtask

  task automatic read_reg(input logic [1:0] a, output logic [31:0] d);
    @(negedge clk);
    addr = a;
    #1 d = rdata;
    $display("tb: read  addr=0x%0h data=0x%0h", a, d);
  endtask

  task automatic check(input string what, input logic [31:0] got, input logic [31:0] expected);
    if (got !== expected) begin
      $display("FAIL %s: expected 0x%0h got 0x%0h (rst_n=%b)", what, expected, got, rst_n);
      errors++;
    end else begin
      $display("ok   %s = 0x%0h", what, got);
    end
  endtask

  initial begin
    repeat (2) @(negedge clk);
    rst_n = 1'b1;
    $display("tb: reset released");

    // REQ-TIM-002: a write to COUNT loads it.
    write_reg(2'd0, 32'd100);
    read_reg(2'd0, value);
    check("COUNT after load", value, 32'd100);

    // REQ-TIM-001: COUNT increments once per enabled cycle, holds while disabled.
    write_reg(2'd0, 32'd0);
    write_reg(2'd2, 32'h1);  // CTRL.EN = 1
    repeat (4) @(negedge clk);
    write_reg(2'd2, 32'h0);  // CTRL.EN = 0
    read_reg(2'd0, value);
    check("COUNT after 6 enabled cycles", value, 32'd6);

    // REQ-TIM-003: irq rises once COUNT reaches COMPARE while enabled, and stays high.
    write_reg(2'd0, 32'd0);
    write_reg(2'd1, 32'd3);
    write_reg(2'd2, 32'h1);
    repeat (6) @(negedge clk);
    check("irq after COUNT reached COMPARE", {31'd0, irq}, 32'h1);

    // REQ-TIM-004: writing 1 to CTRL.IRQ_CLR clears a pending interrupt.
    write_reg(2'd2, 32'h2);  // EN = 0, IRQ_CLR = 1
    check("irq after CTRL.IRQ_CLR write", {31'd0, irq}, 32'h0);

    // REQ-TIM-005: CTRL reads enable in bit 0 and pending in bit 1; offset 0x3 reads 0.
    read_reg(2'd2, value);
    check("CTRL readback after clear", value, 32'h0);
    read_reg(2'd3, value);
    check("offset 0x3", value, 32'h0);

    if (errors == 0) begin
      $display("PASS tb_tiny_timer");
    end else begin
      $display("FAIL tb_tiny_timer: %0d check(s) failed", errors);
      $fatal(1, "tb_tiny_timer failed");
    end
    $finish;
  end

endmodule
