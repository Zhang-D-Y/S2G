module tb;
reg [7:0] A,B; wire [15:0] product; integer i,j,checks;
TopModule dut(A,B,product);
initial begin
checks=0;
for(i=0;i<256;i=i+1) for(j=0;j<256;j=j+1) begin
A=i; B=j; #1;
if(product !== (i*j)) $fatal(1,"S2G_FAIL");
checks=checks+1;
end
$display("S2G_PASS checks=%0d failures=0",checks); $finish;
end
endmodule
