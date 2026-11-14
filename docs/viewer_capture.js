/* Local display capture only. No planner, physics, network or scoring calls. */
(function(root){
  'use strict';
  function supported(){return typeof MediaRecorder!=='undefined' && typeof HTMLCanvasElement.prototype.captureStream==='function' && ['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].some(t=>MediaRecorder.isTypeSupported(t));}
  function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}
  function record(options){
    if(!supported())throw Error('WebM recording is unavailable in this browser. Use current Chrome or Edge, or save a PNG.');
    const {width,height,fps,bitrate,duration,render,onprogress}=options;
    if(![width,height,fps,bitrate,duration].every(Number.isFinite)||width<1||height<1||fps<1||duration<0)throw Error('Invalid display recording settings.');
    const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const ctx=canvas.getContext('2d');
    const mimeType=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].find(t=>MediaRecorder.isTypeSupported(t));
    const stream=canvas.captureStream(fps);let recorder,request=null,timer=null,done=false,rejectPromise,resolvePromise,started=null,last=-Infinity,bytes=0;
    const chunks=[];const promise=new Promise((resolve,reject)=>{resolvePromise=resolve;rejectPromise=reject;});
    function cleanup(){if(request!==null)cancelAnimationFrame(request);if(timer!==null)clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);for(const track of stream.getTracks())track.stop();}
    function cancel(message='Recording cancelled. No video file was saved.'){if(done)return;done=true;if(recorder&&recorder.state!=='inactive')recorder.stop();cleanup();rejectPromise(Error(message));}
    function visibility(){if(document.hidden)cancel('Recording cancelled because the tab became hidden. Keep it visible and try again.');}
    function draw(time){ctx.drawImage(render(time),0,0,width,height);}
    function tick(now){
      if(done)return;if(started===null)started=now;const time=Math.min(duration,(now-started)/1000);
      if(now-last>=1000/fps || time===duration){try{draw(time);onprogress?.(time,duration);}catch(e){cancel('Recording failed: '+e.message);return;}last=now;}
      if(time>=duration){timer=setTimeout(()=>{if(!done&&recorder.state!=='inactive')recorder.stop();},Math.ceil(1000/fps)+60);}
      else request=requestAnimationFrame(tick);
    }
    try{
      recorder=new MediaRecorder(stream,{mimeType,videoBitsPerSecond:bitrate});
      recorder.ondataavailable=e=>{if(done||!e.data.size)return;bytes+=e.data.size;if(bytes>256*1024*1024){cancel('Recording exceeded 256 MB. Choose a lower visual quality or one stage.');return;}chunks.push(e.data);};
      recorder.onerror=e=>cancel('Video encoder error: '+(e.error?.message||'unknown error'));
      recorder.onstop=()=>{if(done)return;done=true;cleanup();const blob=new Blob(chunks,{type:mimeType});if(!blob.size){rejectPromise(Error('The browser produced an empty video.'));return;}resolvePromise(blob);};
      draw(0);document.addEventListener('visibilitychange',visibility);recorder.start(1000);request=requestAnimationFrame(tick);
    }catch(e){cancel(e.message);}
    return {promise,cancel};
  }
  root.AP_VIEWER_CAPTURE={supported,download,record};
})(typeof globalThis!=='undefined'?globalThis:this);
