// SPDX-License-Identifier: Apache-2.0
// CDC fixture: a crossing between two sibling instances through parent wiring.
//
// `u_src` registers `d` in the `clk` domain; its output feeds `u_dst`, which registers
// it in the `clk_b` domain with no synchroniser in between. The crossing is at `u_dst`.
module cdc_dff (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(posedge clk) q <= d;
endmodule

module cdc_hier_cross (
    input  logic clk,
    input  logic clk_b,
    input  logic d
);
  logic src_q;
  logic dst_q;

  cdc_dff u_src (.clk (clk),   .d (d),     .q (src_q));
  cdc_dff u_dst (.clk (clk_b), .d (src_q), .q (dst_q));
endmodule
