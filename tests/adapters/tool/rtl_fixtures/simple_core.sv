// SPDX-License-Identifier: Apache-2.0
// A simple core with parameters and a generate block.

module simple_core
  #(parameter int WIDTH = 8, parameter int DEPTH = 16)
  (input  logic clk,
   input  logic rst_n,
   input  logic enable,
   input  logic [WIDTH-1:0] i_data,
   output logic [WIDTH*2-1:0] o_result);

   logic [WIDTH-1:0] data_q;

   always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n)
        data_q <= '0;
      else if (enable)
        data_q <= i_data;
   end

   assign o_result = {data_q, i_data};

   // Generate block for optional parity checker
   generate
      if (WIDTH > 4) begin : parity_block
         logic parity;
         always_comb parity = ^data_q;
      end
   endgenerate

endmodule
