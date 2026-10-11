// SPDX-License-Identifier: Apache-2.0
// Style exemplar A (chipgraph M2-04): a flag that is set and cleared.

module ex_a_flag (
  input  logic clk,
  input  logic rst_n,
  input  logic set,
  input  logic clear,
  output logic flag
);

  logic flag_d;
  logic flag_q;

  always_comb begin
    flag_d = flag_q;
    if (clear) begin
      flag_d = 1'b0;
    end else if (set) begin
      flag_d = 1'b1;
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      flag_q <= 1'b0;
    end else begin
      flag_q <= flag_d;
    end
  end

  assign flag = flag_q;

endmodule
