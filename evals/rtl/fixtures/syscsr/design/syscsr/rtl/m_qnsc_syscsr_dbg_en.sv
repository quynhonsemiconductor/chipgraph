// chipgraph eval fixture (evals/rtl): the source file follows this header, unchanged.
// source-repo: quynhonsemiconductor/vlsi_deep_training
// source-path: design/syscsr/rtl/m_qnsc_syscsr_dbg_en.sv
// source-commit: d792f03df2a03d01a7977c56def54ea46c580378 (pull request #53)
// source-blob: 1e809b5deee768a49522bdf602d05fd2c4f3c12e
// licence: Apache-2.0
// chipgraph-fixture-end
`timescale 1ns/1ps

// m_qnsc_syscsr_dbg_en -- the DBG_BOOT.dbg_en status bit (QNSC_SYSCSR_MAS 7.5)
//
//   o_dbg_en_masked = i_dbg_en_mask ? 0 : DBG_EN pin through two flip-flops
//
// The DBG_EN pin is asynchronous to i_clk_cpu, so it passes two flip-flops
// before anything reads it. The mask comes from the generated register field
// DBG_BOOT.dbg_en_mask, written 1 by the host over JTAG; it can only force the
// bit to 0, never to 1. The boot ROM waits while o_dbg_en_masked is 1.
//
// Reset by i_rst_n_por only, like the rest of SYSCSR: the flip-flops read 0
// until the pin has been sampled twice after power-on.
module m_qnsc_syscsr_dbg_en (
  input  logic i_clk_cpu,
  input  logic i_rst_n_por,
  input  logic i_dbg_en,          // DBG_EN pad, asynchronous
  input  logic i_dbg_en_mask,     // DBG_BOOT.dbg_en_mask
  output logic o_dbg_en_masked    // DBG_BOOT.dbg_en
);
  logic [1:0] r_dbg_en_sync;

  always_ff @(posedge i_clk_cpu or negedge i_rst_n_por) begin
    if (!i_rst_n_por) r_dbg_en_sync <= 2'b00;
    else              r_dbg_en_sync <= {r_dbg_en_sync[0], i_dbg_en};
  end

  assign o_dbg_en_masked = r_dbg_en_sync[1] & ~i_dbg_en_mask;
endmodule
