
class Button {
 constructor(opts={}){
   this.element=document.createElement('button');
   this.element.innerText=opts.text||'AniTrack';
   this.element.style.padding='14px 24px';
   this.element.style.borderRadius='999px';
   this.element.style.border='1px solid rgba(255,255,255,0.14)';
   this.element.style.background='rgba(255,255,255,0.08)';
   this.element.style.backdropFilter='blur(30px)';
   this.element.style.webkitBackdropFilter='blur(30px)';
   this.element.style.boxShadow='0 10px 40px rgba(0,0,0,.35)';
   this.element.style.color='white';
   this.element.style.cursor='pointer';
   this.element.style.fontWeight='600';
   this.element.onclick=opts.onClick||(()=>{});
 }
}
