// SPDX-License-Identifier: Apache-2.0
// Style exemplar A (chipgraph M2-04): a loadable up counter.
//
// Two-space indent, `begin` at the end of the line, an asynchronous active-low reset,
// registers end in _q and their next values in _d, ports padded so the types line up.

module ex_a_counter #(
  parameter int unsigned Width = 8
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             load,
  input  logic [Width-1:0] load_value,
  output logic [Width-1:0] count
);

  logic [Width-1:0] count_d;
  logic [Width-1:0] count_q;

  always_comb begin
    count_d = count_q + Width'(1);
    if (load) begin
      count_d = load_value;
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count_q <= '0;
    end else begin
      count_q <= count_d;
    end
  end

  assign count = count_q;

endmodule
