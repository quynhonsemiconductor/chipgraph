// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage holdout2: a pin-level test of examples/tinysoc's tiny_gpio.
//
// Not part of tinysoc, and only used by the second holdout set (`evals/triage/holdout2.yml`):
// `evals/triage/gen_logs.py --set holdout2` copies it into a temporary copy of the project
// (as `tb/tb_gpio_pins.sv`) to produce simulation logs with an injected fault.
//
// Walks a one through every bit of DATA_OUT and DIR and checks the pins the clock after
// each write (REQ-GPIO-001, REQ-GPIO-003), then reads DATA_IN back (REQ-GPIO-002). Writes
// carry other data in the bytes above 7:0, which TINY_GPIO_MAS.md says are not meaningful.
// The first failed check ends the run with `$fatal`.
//
// Run: verilator --binary --timing --timescale 1ns/1ps rtl/tiny_gpio.sv tb/tb_gpio_pins.sv \
//   --top-module tb_gpio_pins && obj_dir/Vtb_gpio_pins

module tb_gpio_pins;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 1:0] addr = 2'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic [ 7:0] pin_in = 8'd0;
  logic [ 7:0] pin_out;
  logic [ 7:0] pin_dir;

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

  task automatic write_word(input logic [1:0] a, input logic [31:0] d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(posedge clk);
    #1;
    wr_en = 1'b0;
  endtask

  task automatic expect_pins(input string req, input string pins, input logic [7:0] got,
                             input logic [7:0] want, input logic [31:0] written);
    if (got !== want) begin
      $fatal(1, "tb_gpio_pins: %s: %s=0x%02h after writing 0x%08h, expected 0x%02h", req, pins,
             got, written, want);
    end
  endtask

  initial begin
    logic [31:0] word;
    $display("tb_gpio_pins: start");
    repeat (2) @(negedge clk);
    rst_n = 1'b1;
    expect_pins("reset", "pin_out", pin_out, 8'h00, 32'h0);
    expect_pins("reset", "pin_dir", pin_dir, 8'h00, 32'h0);

    for (int bit_index = 0; bit_index < 8; bit_index++) begin
      word = {8'h5A, 8'hC3, 8'h00, 8'(1 << bit_index)};
      write_word(2'd0, word);
      expect_pins("REQ-GPIO-001", "pin_out", pin_out, 8'(1 << bit_index), word);
      write_word(2'd2, ~word);
      expect_pins("REQ-GPIO-003", "pin_dir", pin_dir, ~8'(1 << bit_index), ~word);
      $display("tb_gpio_pins: bit %0d ok", bit_index);
    end

    pin_in = 8'h3C;
    @(negedge clk);
    addr = 2'd1;
    #1 word = rdata;
    if (word !== 32'h0000_003C) begin
      $fatal(1, "tb_gpio_pins: REQ-GPIO-002: DATA_IN read 0x%08h with pin_in=0x3c", word);
    end

    $display("tb_gpio_pins: PASS");
    $finish;
  end

endmodule
