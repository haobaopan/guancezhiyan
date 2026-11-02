
(function(){
  var needle=window.__KGSZ_NEEDLE__||''; if(!needle) return;
  var norm=needle.replace(/\s+/g,''); if(norm.length<6) return;
  var nodes=[],parts=[];
  var walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null),n;
  while((n=walker.nextNode())){
    if(n.parentNode&&n.parentNode.closest&&n.parentNode.closest('.kgsz-bar')) continue;
    var s=n.nodeValue||'',t='',m=[];
    for(var i=0;i<s.length;i++){var c=s.charAt(i);if(!/\s/.test(c)){t+=c;m.push(i);}}
    if(!t) continue;
    nodes.push(n); parts.push({t:t,m:m});
  }
  if(!parts.length) return;
  var full=parts.map(function(p){return p.t;}).join('');
  var idx=-1,hit=0;
  var cands=[32,20,12,8];
  for(var ci=0;ci<cands.length;ci++){
    hit=Math.min(cands[ci],norm.length);
    idx=full.indexOf(norm.slice(0,hit));
    if(idx>=0) break;
  }
  if(idx<0) return;
  var end=Math.min(idx+norm.length,full.length);
  function locate(g){
    var acc=0;
    for(var k=0;k<parts.length;k++){
      var len=parts[k].t.length;
      if(g<acc+len) return {k:k,off:g-acc};
      acc+=len;
    }
    return {k:parts.length-1,off:parts[parts.length-1].t.length-1};
  }
  function realOff(p,o){
    if(o<0) return 0;
    if(o>=p.m.length) return p.m[p.m.length-1]+1;
    return p.m[o];
  }
  var A=locate(idx),B=locate(Math.max(idx,end-1));
  try{
    var r=document.createRange();
    r.setStart(nodes[A.k],realOff(parts[A.k],A.off));
    r.setEnd(nodes[B.k],realOff(parts[B.k],B.off+1));
    var frag=r.extractContents();
    var mk=document.createElement('mark'); mk.className='kgsz-hit';
    mk.appendChild(frag);
    r.insertNode(mk);
    mk.scrollIntoView({block:'start'});
  }catch(e){}
})();
