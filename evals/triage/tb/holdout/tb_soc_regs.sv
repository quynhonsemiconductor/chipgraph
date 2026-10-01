// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage holdout: a self-checking testbench for examples/tinysoc's tiny_top.
//
// Not part of tinysoc, and only used by the holdout set (`evals/triage/holdout.yml`):
// `evals/triage/gen_logs.py --set holdout` copies it into a temporary copy of the project
// (as `tb/tb_soc_regs.sv`) to produce simulation logs with an injected fault.
//
// It drives the top-level register bus (chip.yml: timer at word 0x0, gpio at word 0x4,
// `addr[3:2]` selects the block) and checks the MAS behaviour through it: reset values,
// REQ-GPIO-001..003, REQ-TIM-001/003/004, and that an unmapped window reads 0. A failed
// check is an immediate assertion (`$error`); any failure ends with `$fatal`.
//
// Run: verilator --binary --timing --timescale 1ns/1ps -f filelists/top.f tb/tb_soc_regs.sv \
//   --top-module tb_soc_regs && obj_dir/Vtb_soc_regs

module tb_soc_regs;

  localparam int unsigned IrqWaitCycles = 40;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 3:0] addr = 4'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic [ 7:0] gpio_pin_in = 8'd0;
  logic [ 7:0] gpio_pin_out;
  logic [ 7:0] gpio_pin_dir;
  logic        timer_irq;

  int unsigned failures = 0;
  logic [31:0] word;

  always #5 clk = ~clk;

  tiny_top dut (
      .clk         (clk),
      .rst_n       (rst_n),
      .addr        (addr),
      .wr_en       (wr_en),
      .wdata       (wdata),
      .rdata       (rdata),
      .gpio_pin_in (gpio_pin_in),
      .gpio_pin_out(gpio_pin_out),
      .gpio_pin_dir(gpio_pin_dir),
      .timer_irq   (timer_irq)
  );

  // One bus write: address and data set up on a falling edge, captured on the next rising
  // edge, strobe dropped on the falling edge after it.
  task automatic bus_write(input logic [3:0] a, input logic [31:0] d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(negedge clk);
    wr_en = 1'b0;
    $display("[%0t] bus write 0x%h <= 0x%08h", $time, a, d);
  endtask

  task automatic bus_read(input logic [3:0] a, output logic [31:0] d);
    @(negedge clk);
    addr = a;
    #1 d = rdata;
    $display("[%0t] bus read  0x%h => 0x%08h", $time, a, d);
  endtask

  task automatic expect_eq(input string what, input logic [31:0] got, input logic [31:0] want);
    assert (got === want)
    else begin
      failures++;
      $error("%s: read 0x%08h, want 0x%08h", what, got, want);
    end
  endtask

  initial begin
    repeat (3) @(negedge clk);
    rst_n = 1'b1;
    $display("[%0t] reset deasserted", $time);

    // Reset values (both MAS, section 6): everything reads 0.
    bus_read(4'h0, word);
    expect_eq("timer COUNT after reset", word, 32'h0);
    bus_read(4'h2, word);
    expect_eq("timer CTRL after reset", word, 32'h0);
    expect_eq("timer enable flop after reset", {31'd0, dut.u_timer.enable_q}, 32'h0);
    bus_read(4'h4, word);
    expect_eq("gpio DATA_OUT after reset", word, 32'h0);
    bus_read(4'h6, word);
    expect_eq("gpio DIR after reset", word, 32'h0);

    // REQ-GPIO-001: DATA_OUT reaches gpio_pin_out from the clock that captures the write.
    @(negedge clk);
    addr  = 4'h4;
    wdata = 32'h0000_003C;
    wr_en = 1'b1;
    @(posedge clk);
    #1;
    expect_eq("gpio_pin_out one clock after the DATA_OUT write", {24'd0, gpio_pin_out}, 32'h3C);
    @(negedge clk);
    wr_en = 1'b0;

    // REQ-GPIO-003: DIR reaches gpio_pin_dir.
    bus_write(4'h6, 32'h0000_00F0);
    expect_eq("gpio_pin_dir after the DIR write", {24'd0, gpio_pin_dir}, 32'hF0);

    // REQ-GPIO-002: DATA_IN reads gpio_pin_in in bits 7:0, 0 above.
    gpio_pin_in = 8'h81;
    bus_read(4'h5, word);
    expect_eq("gpio DATA_IN with gpio_pin_in=0x81", word, 32'h81);

    // REQ-TIM-001/003: enabled, COUNT runs up to COMPARE and timer_irq rises.
    bus_write(4'h1, 32'd4);
    bus_write(4'h2, 32'h1);
    fork
      begin : wait_irq
        wait (timer_irq === 1'b1);
        $display("[%0t] timer_irq rose", $time);
      end
      begin : watchdog
        repeat (IrqWaitCycles) @(posedge clk);
        failures++;
        $error("timer_irq did not rise within %0d cycles of CTRL.EN=1", IrqWaitCycles);
      end
    join_any
    disable fork;

    // REQ-TIM-004: writing 1 to CTRL.IRQ_CLR (with EN=0) drops timer_irq.
    bus_write(4'h2, 32'h2);
    expect_eq("timer_irq after CTRL.IRQ_CLR", {31'd0, timer_irq}, 32'h0);

    // Words 0x8..0xF belong to no block (chip.yml): they read 0.
    bus_read(4'h8, word);
    expect_eq("unmapped word 0x8", word, 32'h0);

    if (failures == 0) begin
      $display("tb_soc_regs: all checks passed");
    end else begin
      $fatal(1, "tb_soc_regs: %0d check(s) failed", failures);
    end
    $finish;
  end

endmodule
