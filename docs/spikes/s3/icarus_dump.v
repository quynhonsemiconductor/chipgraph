// S3 spike: waveform dump for the Icarus flow (a second top level next to tiny_gpio).
`timescale 1ns / 1ps
module s3_dump;
  initial begin
    $dumpfile("dump.fst");
    $dumpvars(0, tiny_gpio);
  end
endmodule
