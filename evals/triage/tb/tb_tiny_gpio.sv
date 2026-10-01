// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage: a self-checking testbench for examples/tinysoc's tiny_gpio.
//
// Not part of tinysoc: `evals/triage/gen_logs.py` copies it into a temporary copy of the
// project (as `tb/tb_tiny_gpio.sv`) to produce simulation logs, with or without an
// injected fault. Checks follow TINY_GPIO_MAS.md: reset values, REQ-GPIO-001..003.
//
// Run: verilator --binary --timing --timescale 1ns/1ps rtl/tiny_gpio.sv tb/tb_tiny_gpio.sv \
//   --top-module tb_tiny_gpio && obj_dir/Vtb_tiny_gpio

module tb_tiny_gpio;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 1:0] addr = 2'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic [ 7:0] pin_in = 8'd0;
  logic [ 7:0] pin_out;
  logic [ 7:0] pin_dir;

  int          errors = 0;
  logic [31:0] value;

  always #5 clk = ~clk;

  tiny_gpio dut (
      .clk    (clk),
      .rst_n  (rst_n),
      .addr   (addr),
      .wr_en  (wr_en),
      .wdata  (wdata),
      .rdata  (rdata),
      .pin_in (pin_in),
      .pin_out(pin_out),
      .pin_dir(pin_dir)
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

    // Reset values (MAS section 6).
    read_reg(2'd0, value);
    check("DATA_OUT after reset", value, 32'h0);
    read_reg(2'd2, value);
    check("DIR after reset", value, 32'h0);

    // REQ-GPIO-001: a write to DATA_OUT appears on pin_out.
    write_reg(2'd0, 32'hA5);
    check("pin_out after DATA_OUT write", {24'd0, pin_out}, 32'hA5);

    // REQ-GPIO-002: DATA_IN reads pin_in in bits 7:0.
    pin_in = 8'h5A;
    read_reg(2'd1, value);
    check("DATA_IN with pin_in=0x5a", value, 32'h5A);

    // REQ-GPIO-003: a write to DIR appears on pin_dir; offset 0x3 reads 0.
    write_reg(2'd2, 32'h0F);
    check("pin_dir after DIR write", {24'd0, pin_dir}, 32'h0F);
    read_reg(2'd3, value);
    check("offset 0x3", value, 32'h0);

    if (errors == 0) begin
      $display("PASS tb_tiny_gpio");
    end else begin
      $display("FAIL tb_tiny_gpio: %0d check(s) failed", errors);
      $fatal(1, "tb_tiny_gpio failed");
    end
    $finish;
  end

endmodule
