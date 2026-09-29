// SPDX-License-Identifier: Apache-2.0
// Emacs verilog-mode AUTO wrapper for a simple module.

module wrapper
  (/*AUTOARG*/
   // Outputs
   o_result,
   // Inputs
   i_clk, i_rst_n, i_enable, i_data
   );

   input  logic       i_clk;
   input  logic       i_rst_n;
   input  logic       i_enable;
   input  logic [7:0] i_data;
   output logic [15:0] o_result;

   // /*AUTOINST*/
   simple_core core_i
     (.i_clk   (i_clk),
      .i_rst_n (i_rst_n),
      .i_enable(i_enable),
      .i_data  (i_data),
      .o_result(o_result));

endmodule
