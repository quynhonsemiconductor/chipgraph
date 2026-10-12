// chipgraph eval fixture (evals/rtl): the source file follows this header, unchanged.
// source-repo: quynhonsemiconductor/vlsi_deep_training
// source-path: design/common/rtl/qnsc_sync.sv
// source-commit: 197927f1784cf36a205d9c56d1b35bf5fb5f9c2a (pull request #26)
// source-blob: 24f1bd98cae41c71af043f9a18837cfc223b2fa5
// licence: Apache-2.0
// chipgraph-fixture-end
`timescale 1ns/1ps

// Flip-flop synchroniser for level signals entering the i_clk_dst domain.
//
// Each bit is synchronised on its own, so use it for independent levels (a pad,
// a request flag), never for a multi-bit value that must arrive together -- that
// needs a handshake. Every such crossing in QSOC goes through this cell, so a
// crossing is found with `grep qnsc_sync` (CONTRIBUTING.md, CDC stage).
//
// Users: PWM (TIM_EXT pads), SYSDBG (requests, DBG_EN).

module qnsc_sync #(
  parameter int unsigned        P_WIDTH       = 1,
  parameter int unsigned        P_STAGES      = 2,
  parameter logic [P_WIDTH-1:0] P_RST_VALUE = '0
) (
  input  logic               i_clk_dst,
  input  logic               i_rst_n_dst,
  input  logic [P_WIDTH-1:0] i_d,
  output logic [P_WIDTH-1:0] o_q
);

  logic [P_STAGES*P_WIDTH-1:0] r_sync;

  always_ff @(posedge i_clk_dst or negedge i_rst_n_dst) begin
    if (!i_rst_n_dst) begin
      r_sync <= {P_STAGES{P_RST_VALUE}};
    end else begin
      r_sync <= {r_sync[(P_STAGES-1)*P_WIDTH-1:0], i_d};
    end
  end

  assign o_q = r_sync[P_STAGES*P_WIDTH-1 -: P_WIDTH];

endmodule
