// A clean module: every declared identifier follows the QNSC naming rule.
module m_qnsc_timer #(
  parameter int P_WIDTH = 8,
  localparam int C_MAX = 255
) (
  input  logic i_clk,
  input  logic i_rst_n,
  output logic o_int,
  inout  wire  io_bus
);

  logic       r_count;
  logic       w_next;
  logic [7:0] mem_buffer [0:15];

  typedef enum logic [1:0] { S_IDLE, S_RUN, S_DONE } state_e;

  genvar gi;

  m_qnsc_sub u_sub ( .i_clk(i_clk) );

endmodule
