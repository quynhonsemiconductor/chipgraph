module ex_b_counter #(
    parameter int unsigned Width = 8
) (
    input logic clk,
    input logic rst_n,
    input logic load,
    input logic [Width-1:0] load_value,
    output logic [Width-1:0] count
);


    logic [Width-1:0] count_next;
    logic [Width-1:0] count_r;


    // The count steps by one every clock; a load replaces it with load_value on the next clock.
    always_comb
    begin
        count_next = count_r + Width'(1);
        if (load)
        begin
            count_next = load_value;
        end
    end


    always_ff @(posedge clk)
    begin
        if (!rst_n)
        begin
            count_r <= '0;
        end
        else
        begin
            count_r <= count_next;
        end
    end


    assign count = count_r;

endmodule
