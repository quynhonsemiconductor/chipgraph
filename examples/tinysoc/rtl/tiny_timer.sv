// SPDX-License-Identifier: Apache-2.0
// chipgraph example: tiny_timer
//
// A minimal free-running counter with a compare register and a level interrupt.
// Register map (word-addressed, 2-bit `addr`):
//   0x0  COUNT    (RW) free-running counter, resets to 0
//   0x1  COMPARE  (RW) IRQ asserts once COUNT reaches COMPARE while enabled
//   0x2  CTRL     (RW) bit0 = enable; write bit1 = 1 to clear a pending IRQ
//                 (RO) bit0 readback = enable, bit1 readback = irq pending

module tiny_timer (
    input  logic        clk,
    input  logic        rst_n,
    input  logic [1:0]  addr,
    input  logic         wr_en,
    input  logic [31:0]  wdata,
    output logic [31:0]  rdata,
    output logic         irq
);

  logic [31:0] count_q;
  logic [31:0] compare_q;
  logic        enable_q;
  logic        irq_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count_q   <= 32'd0;
      compare_q <= 32'd0;
      enable_q  <= 1'b0;
      irq_q     <= 1'b0;
    end else begin
      if (enable_q) begin
        count_q <= count_q + 32'd1;
      end

      if (wr_en) begin
        unique case (addr)
          2'd0: count_q <= wdata;
          2'd1: compare_q <= wdata;
          2'd2: begin
            enable_q <= wdata[0];
            if (wdata[1]) begin
              irq_q <= 1'b0;
            end
          end
          default: ;
        endcase
      end

      if (enable_q && count_q == compare_q) begin
        irq_q <= 1'b1;
      end
    end
  end

  always_comb begin
    unique case (addr)
      2'd0:    rdata = count_q;
      2'd1:    rdata = compare_q;
      2'd2:    rdata = {30'd0, irq_q, enable_q};
      default: rdata = 32'd0;
    endcase
  end

  assign irq = irq_q;

endmodule
