// SPDX-License-Identifier: Apache-2.0
// CDC fixture: two sibling instances in the SAME clock domain -- no crossing.
//
// Both `u_src` and `u_dst` are clocked by `clk`, so passing `src_q` from one to the
// other is not a domain crossing and the check must not flag it.
module cdc_dff2 (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(posedge clk) q <= d;
endmodule

module cdc_hier_same (
    input  logic clk,
    input  logic d
);
  logic src_q;
  logic dst_q;

  cdc_dff2 u_src (.clk (clk), .d (d),     .q (src_q));
  cdc_dff2 u_dst (.clk (clk), .d (src_q), .q (dst_q));
endmodule
