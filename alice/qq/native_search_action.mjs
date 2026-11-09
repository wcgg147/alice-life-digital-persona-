// ALICE_QQ_NATIVE_SEARCH_BEGIN
defineAction({
 name: "qq_search_native_expressions",
 summary: "QQ native panel expression search and GIF recommendations",
 readOnly: true,
 params: {
  query: f$1.string().default(""),
  tab: f$1.string().default("all")
 },
 returnsSchema: { type: "object" },
 run: async (p, ctx) => {
  const query=String(p.query).trim(), tab=String(p.tab);
  if (!["all","composed","gif"].includes(tab) || query.length>24 || /\d{5,}|https?:\/\/|@|[\r\n]/.test(query) || (!query && tab!=="gif")) {
   throw new Error("use_short_public_expression_keywords");
  }
  const vi=(n)=>{const b=[]; do {const v=n%128; n=Math.floor(n/128); b.push(v|(n?128:0));}while(n); return Buffer.from(b);};
  const uint=(f,n)=>Buffer.concat([vi(f*8),vi(n)]);
  const bytes=(f,value)=>{const b=Buffer.isBuffer(value)?value:Buffer.from(value);return Buffer.concat([vi(f*8+2),vi(b.length),b]);};
  const uin=Number(ctx.bridge.identity.uin), uid=ctx.bridge.identity.selfUid;
  if (!Number.isSafeInteger(uin) || uin<=0 || !/^u_[A-Za-z0-9_-]{22}$/.test(uid||"")) throw new Error("QQ_identity_not_ready");
  let body,command;
  if (tab==="gif" && !query) {
   // Captured from this QQ build's GIF panel (native category 35, 36 results).
   body=bytes(1,Buffer.concat([bytes(1,"nt_1"),uint(2,uin),uint(3,1),uint(4,0),uint(5,2),uint(6,0),uint(7,35),uint(8,1),bytes(9,"8.8.28.0")]));
   command="ImagePopular.GetInfo";
  } else {
   // Native getHotPicSearchResult: tab 8 = all, 9 = text-composition templates.
   const request=Buffer.concat([uint(1,uin),bytes(2,query),uint(3,36),uint(4,0),uint(6,tab==="composed"?9:8),uint(7,1),bytes(8,uid),uint(9,0),uint(10,0),uint(11,0),uint(12,1),uint(13,1)]);
   body=Buffer.concat([uint(1,0x9038),uint(2,1),uint(3,0),bytes(4,request),bytes(5,""),uint(12,0)]);
   command="OidbSvcTrpcTcp.0x9038_1";
  }
  const result=await ctx.bridge.sendRawPacket(command,body,8000);
  const data=result.responseData?Buffer.from(result.responseData):Buffer.alloc(0);
  if(data.length>2097152)throw new Error("native_search_response_too_large");
  return okResponse({source:"qq_native_panel",tab,native_success:result.success,got_response:result.gotResponse,
   native_error:result.errorCode,response_bytes:data.length,body_base64:data.toString("base64")});
 }
}),
// ALICE_QQ_NATIVE_SEARCH_END
