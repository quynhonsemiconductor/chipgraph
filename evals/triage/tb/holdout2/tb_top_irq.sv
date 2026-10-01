// SPDX-License-Identifier: Apache-2.0
// chipgraph evals/triage holdout2: an interrupt test of examples/tinysoc's tiny_top.
//
// Not part of tinysoc, and only used by the second holdout set (`evals/triage/holdout2.yml`):
// `evals/triage/gen_logs.py --set holdout2` copies it into a temporary copy of the project
// (as `tb/tb_top_irq.sv`) to produce simulation logs with an injected fault.
//
// Through the top-level register bus (chip.yml: timer at word 0x0, `addr[3:2]` selects the
// block) it loads COUNT and COMPARE, sets CTRL.EN and waits for `timer_irq` under a
// watchdog (REQ-TIM-001/003), then clears it (REQ-TIM-004). A watchdog timeout or a failed
// check ends the run at once with `$fatal`.
//
// Run: verilator --binary --timing --timescale 1ns/1ps -f filelists/top.f tb/tb_top_irq.sv \
//   --top-module tb_top_irq && obj_dir/Vtb_top_irq

module tb_top_irq;

  localparam logic [31:0] CompareValue = 32'd20;
  // COUNT needs COMPARE + 1 enabled cycles to raise the interrupt; leave a margin.
  localparam int unsigned IrqTimeoutCycles = 64;

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

  logic [31:0] count;
  logic [31:0] ctrl;
  int unsigned waited = 0;

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

  task automatic reg_write(input logic [3:0] a, input logic [31:0] d);
    @(negedge clk);
    addr  = a;
    wdata = d;
    wr_en = 1'b1;
    @(negedge clk);
    wr_en = 1'b0;
    $display("tb_top_irq: [%0t] write word 0x%h = 0x%08h", $time, a, d);
  endtask

  task automatic reg_read(input logic [3:0] a, output logic [31:0] d);
    @(negedge clk);
    addr = a;
    #1 d = rdata;
    $display("tb_top_irq: [%0t] read  word 0x%h = 0x%08h", $time, a, d);
  endtask

  initial begin
    $display("tb_top_irq: start");
    repeat (2) @(negedge clk);
    rst_n = 1'b1;

    reg_write(4'h0, 32'd0);  // timer COUNT
    reg_write(4'h1, CompareValue);  // timer COMPARE
    reg_write(4'h2, 32'h1);  // timer CTRL.EN = 1

    fork
      begin : wait_irq
        while (timer_irq !== 1'b1) begin
          @(posedge clk);
          waited++;
        end
      end
      begin : watchdog
        repeat (IrqTimeoutCycles) @(posedge clk);
      end
    join_any
    disable fork;

    if (timer_irq !== 1'b1) begin
      reg_read(4'h0, count);
      reg_read(4'h2, ctrl);
      $fatal(1, "tb_top_irq: TIMEOUT: timer_irq still low %0d cycles after CTRL.EN=1 (COMPARE=%0d, COUNT=%0d, CTRL=0x%0h)",
             IrqTimeoutCycles, CompareValue, count, ctrl);
    end
    $display("tb_top_irq: timer_irq rose %0d cycles after CTRL.EN=1", waited);

    reg_write(4'h2, 32'h2);  // CTRL.IRQ_CLR = 1, EN = 0
    if (timer_irq !== 1'b0) begin
      $fatal(1, "tb_top_irq: timer_irq still high after a CTRL.IRQ_CLR write");
    end

    $display("tb_top_irq: PASS");
    $finish;
  end

endmodule
