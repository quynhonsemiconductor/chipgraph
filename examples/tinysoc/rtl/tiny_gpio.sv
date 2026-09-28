// SPDX-License-Identifier: Apache-2.0
// chipgraph example: tiny_gpio
//
// A minimal 8-bit GPIO block: an output data register, an input data readback, and a
// per-bit direction register (1 = output, 0 = input; the actual pad tri-state is left
// to the pad ring, so this block only exposes `pin_out`/`pin_dir` alongside `pin_in`).
// Register map (word-addressed, 2-bit `addr`):
//   0x0  DATA_OUT (RW) driven onto `pin_out`
//   0x1  DATA_IN  (RO) current value of `pin_in`
//   0x2  DIR      (RW) per-bit direction, driven onto `pin_dir`

module tiny_gpio (
    input  logic        clk,
    input  logic        rst_n,
    input  logic [1:0]  addr,
    input  logic         wr_en,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0]  wdata,  // only wdata[7:0] is meaningful for this 8-bit block
    /* verilator lint_on UNUSEDSIGNAL */
    output logic [31:0]  rdata,
    input  logic [7:0]   pin_in,
    output logic [7:0]   pin_out,
    output logic [7:0]   pin_dir
);

  logic [7:0] out_q;
  logic [7:0] dir_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      out_q <= 8'd0;
      dir_q <= 8'd0;
    end else if (wr_en) begin
      unique case (addr)
        2'd0: out_q <= wdata[7:0];
        2'd2: dir_q <= wdata[7:0];
        default: ;
      endcase
    end
  end

  always_comb begin
    unique case (addr)
      2'd0:    rdata = {24'd0, out_q};
      2'd1:    rdata = {24'd0, pin_in};
      2'd2:    rdata = {24'd0, dir_q};
      default: rdata = 32'd0;
    endcase
  end

  assign pin_out = out_q;
  assign pin_dir = dir_q;

endmodule
