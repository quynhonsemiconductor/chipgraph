// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage holdout2: a scoreboard test of examples/tinysoc's tiny_top.
//
// Not part of tinysoc, and only used by the second holdout set (`evals/triage/holdout2.yml`):
// `evals/triage/gen_logs.py --set holdout2` copies it into a temporary copy of the project
// (as `tb/tb_top_scoreboard.sv`) to produce simulation logs with an injected fault.
//
// A fixed sequence of register writes goes through the top-level bus (chip.yml: timer at
// word 0x0, gpio at word 0x4); a reference model of the register map (both MAS, section 6)
// predicts every read-back and the gpio pins. Each disagreement is a `MISMATCH` line; at
// the end a failed run prints `TEST FAILED` and stops with `$stop`.
//
// Run: verilator --binary --timing --timescale 1ns/1ps -f filelists/top.f \
//   tb/tb_top_scoreboard.sv --top-module tb_top_scoreboard && obj_dir/Vtb_top_scoreboard

module tb_top_scoreboard;

  logic        clk = 1'b0;
  logic        rst_n = 1'b0;
  logic [ 3:0] addr = 4'd0;
  logic        wr_en = 1'b0;
  logic [31:0] wdata = 32'd0;
  logic [31:0] rdata;
  logic [ 7:0] gpio_pin_in = 8'h00;
  logic [ 7:0] gpio_pin_out;
  logic [ 7:0] gpio_pin_dir;
  logic        timer_irq;

  // Reference model: the registers the sequence touches. The timer stays disabled, so
  // COUNT and COMPARE only change when written.
  logic [31:0] model_count = 32'd0;
  logic [31:0] model_compare = 32'd0;
  logic [31:0] model_data_out = 32'd0;
  logic [31:0] model_dir = 32'd0;

  int unsigned checks = 0;
  int unsigned mismatches = 0;

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

  function automatic logic [31:0] predict(input logic [3:0] a);
    unique case (a)
      4'h0: return model_count;
      4'h1: return model_compare;
      4'h4: return model_data_out;
      4'h5: return {24'd0, gpio_pin_in};
      4'h6: return model_dir;
      default: return 32'd0;
    endcase
  endfunction

  task automatic model_write(input logic [3:0] a, input logic [31:0] d);
    unique case (a)
      4'h0: model_count = d;
      4'h1: model_compare = d;
      4'h4: model_data_out = {24'd0, d[7:0]};  // DATA_OUT holds wdata[7:0]
      4'h6: model_dir = {24'd0, d[7:0]};  // DIR holds wdata[7:0]
      default: ;
    endcase
  endtask

  task automatic compare(input string what, input logic [31:0] dut_value,
                         input logic [31:0] model_value);
    checks++;
    if (dut_value !== model_value) begin
      mismatches++;
      $display("tb_top_scoreboard: MISMATCH %s: dut=0x%08h model=0x%08h", what, dut_value,
               model_value);
    end
  endtask

  task automatic bus_write(input logic [3:0] a, input logic [31:0] d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(negedge clk);
    wr_en = 1'b0;
    model_write(a, d);
    $display("tb_top_scoreboard: write 0x%h <= 0x%08h", a, d);
  endtask

  task automatic bus_check(input logic [3:0] a);
    logic [31:0] got;
    @(negedge clk);
    addr = a;
    #1 got = rdata;
    $display("tb_top_scoreboard: read  0x%h => 0x%08h", a, got);
    compare($sformatf("read 0x%h", a), got, predict(a));
  endtask

  task automatic pins_check();
    compare("gpio_pin_out", {24'd0, gpio_pin_out}, {24'd0, model_data_out[7:0]});
    compare("gpio_pin_dir", {24'd0, gpio_pin_dir}, {24'd0, model_dir[7:0]});
  endtask

  initial begin
    $display("tb_top_scoreboard: start");
    repeat (2) @(negedge clk);
    rst_n = 1'b1;

    for (int a = 0; a < 16; a++) begin
      if (a != 5) bus_check(4'(a));
    end

    bus_write(4'h1, 32'h0000_1234);
    bus_write(4'h4, 32'hC3C3_C35A);
    bus_write(4'h6, 32'h0000_000F);
    pins_check();
    gpio_pin_in = 8'h96;
    bus_check(4'h1);
    bus_check(4'h4);
    bus_check(4'h5);
    bus_check(4'h6);

    bus_write(4'h0, 32'h0000_0042);
    bus_write(4'h4, 32'h0000_00A5);
    bus_write(4'h6, 32'hFFFF_FF3C);
    pins_check();
    bus_check(4'h0);
    bus_check(4'h4);
    bus_check(4'h6);
    bus_check(4'h7);
    bus_check(4'hC);

    $display("tb_top_scoreboard: %0d checks, %0d mismatch(es)", checks, mismatches);
    if (mismatches != 0) begin
      $display("tb_top_scoreboard: TEST FAILED");
      $stop;
    end
    $display("tb_top_scoreboard: TEST PASSED");
    $finish;
  end

endmodule
