module adder8(input [7:0] a,b, output [8:0] y);
  wire [8:0] carry;
  assign carry[0] = 1'b0;
  genvar i;
  generate for (i=0;i<8;i=i+1) begin: bitslice
    assign y[i] = a[i] ^ b[i] ^ carry[i];
    assign carry[i+1] = (a[i]&b[i]) | ((a[i]^b[i])&carry[i]);
  end endgenerate
  assign y[8] = carry[8];
endmodule
