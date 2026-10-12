// chipgraph eval fixture (evals/rtl): the source file follows this header, unchanged.
// source-repo: quynhonsemiconductor/vlsi_deep_training
// source-path: design/rom/rtl/m_qnsc_rom_wr_resp.sv
// source-commit: 58d1d91e858036276c387ad240063b72ab96c7e0 (pull request #51)
// source-blob: 0093761893c2349017bbe53676f53c4b037fe51c
// licence: Apache-2.0
// chipgraph-fixture-end
// =============================================================================
// m_qnsc_rom_wr_resp -- write-error responder of the boot ROM (QNSC_ROM_MAS 7.3)
//
// The controller's write channel is tied idle, so no write reaches the image. A
// write to the ROM must still be answered, or its master waits forever:
//
//   1. S_IDLE: AWREADY = 1. On the AW handshake, store AWID.
//   2. S_DATA: WREADY = 1. Discard beats up to the one with WLAST.
//   3. S_RESP: BVALID = 1, BID = stored ID, BRESP = SLVERR, until BREADY.
//
// W is accepted only after AW, which AXI allows. Reads are not affected. A stray
// write ends as a store access fault in Ibex, or an error at SYSDBG or DMA.
// Resets to S_IDLE (MAS 7.4).
//
// Plain logic, written by hand (doc/guides/EMACS_AUTO.md: an FSM is not
// generated); instantiated by m_qnsc_wrap_rom, whose ports carry the same
// names, so its AUTOINST connects it without a template. The ports it does not
// use are the wrapper's AW/W ports, which must exist on the AXI interface (MAS
// 5, QNSC_RAM_MAS Table 5-1).
// =============================================================================
module m_qnsc_rom_wr_resp (
  input  logic        i_clk_mem,
  input  logic        i_rst_n_mem,
  // AW
  input  logic [31:0] i_bus_axi_aw_addr,     // not used
  input  logic        i_bus_axi_aw_valid,
  output logic        o_bus_axi_aw_ready,
  input  logic [1:0]  i_bus_axi_aw_burst,    // not used
  input  logic [7:0]  i_bus_axi_aw_len,      // not used: the burst ends at WLAST
  input  logic [6:0]  i_bus_axi_aw_id,
  // W
  input  logic [31:0] i_bus_axi_w_data,      // not used
  input  logic [3:0]  i_bus_axi_w_strb,      // not used
  input  logic        i_bus_axi_w_valid,
  output logic        o_bus_axi_w_ready,
  input  logic        i_bus_axi_w_last,
  // B
  output logic [6:0]  o_bus_axi_b_id,
  output logic [1:0]  o_bus_axi_b_resp,      // always 2'b10, SLVERR
  output logic        o_bus_axi_b_valid,
  input  logic        i_bus_axi_b_ready
);

  typedef enum logic [1:0] {
    S_IDLE = 2'd0,
    S_DATA = 2'd1,
    S_RESP = 2'd2
  } wr_state_t;

  wr_state_t  r_wr_state;
  logic [6:0] r_bid;

  always_ff @(posedge i_clk_mem or negedge i_rst_n_mem) begin
    if (!i_rst_n_mem) begin
      r_wr_state <= S_IDLE;
      r_bid      <= 7'd0;
    end else begin
      case (r_wr_state)
        S_IDLE: if (i_bus_axi_aw_valid) begin
                  r_bid      <= i_bus_axi_aw_id;
                  r_wr_state <= S_DATA;
                end
        S_DATA: if (i_bus_axi_w_valid && i_bus_axi_w_last) r_wr_state <= S_RESP;
        S_RESP: if (i_bus_axi_b_ready)                      r_wr_state <= S_IDLE;
        default:                                            r_wr_state <= S_IDLE;
      endcase
    end
  end

  assign o_bus_axi_aw_ready = (r_wr_state == S_IDLE);
  assign o_bus_axi_w_ready  = (r_wr_state == S_DATA);
  assign o_bus_axi_b_valid  = (r_wr_state == S_RESP);
  assign o_bus_axi_b_id     = r_bid;
  assign o_bus_axi_b_resp   = 2'b10;

endmodule
