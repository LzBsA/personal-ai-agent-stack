# -*- coding: utf-8 -*-
"""从 app.py 中抽出面板 HTML, 注入一个假的 fetch 后端, 生成可离线预览的页面。
保证 CSS/HTML/JS 与线上完全一致, 只是把后端换成了模拟数据。

用法:  python gen_preview.py        # 在 ysunet-tools 目录下执行
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ysunet-app", "app.py")
OUT = os.path.join(HERE, "ysunet-app", "panel-preview.html")

src = open(SRC, encoding="utf-8").read()
html = re.search(r'HTML = """(.*?)"""', src, re.S).group(1)

MOCK = r"""
<script>
/* ===== 仅用于离线预览的模拟后端 (线上 app.py 里没有这段) ===== */
var MOCK={online:false,window:false,checking:false,paused:false,manual:false,
  last_manual:null,manual_ok:null,manual_msg:null,
  last_check:'2026-09-20 13:44:30',last_change:null,
  last_ok:null,last_fail:'2026-09-20 13:44:06',username:'demo-user',service:'中国联通',
  window_start:'23:30',window_end:'05:30',
  error:'Network connection failed'};
var MOCK_LOGS=['[2026-09-20 13:44:01] 已载入持久化配置 (data/config.json)',
  '[2026-09-20 13:44:01] ysunet-panel 启动, 每 300 秒检测一次',
  '[2026-09-20 13:44:01] 服务: 中国联通  账号: demo-user  断网窗口: 23:30-05:30',
  '[2026-09-20 13:44:01] Web 面板已启动: http://0.0.0.0:8080',
  '[2026-09-20 13:44:01] 外网不通，开始认证...',
  '[2026-09-20 13:44:06] 认证失败: Network connection failed，下轮重试'];
function mockNow(){var d=new Date();function p(n){return (n<10?'0':'')+n;}
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());}
function mockLog(t){MOCK_LOGS.push('['+mockNow()+'] '+t);if(MOCK_LOGS.length>60)MOCK_LOGS.shift();}
function mockJson(o,code){return Promise.resolve({status:code||200,ok:(code||200)===200,
  json:function(){return Promise.resolve(JSON.parse(JSON.stringify(o)));}});}
window.fetch=function(url,opt){
  opt=opt||{};
  var path=String(url).split('?')[0];
  var method=(opt.method||'GET').toUpperCase();
  if(path==='/api/status')return mockJson(MOCK);
  if(path==='/api/logs')return mockJson({lines:MOCK_LOGS.slice(-60)});
  if(path==='/api/settings')return mockJson({username:MOCK.username,service:MOCK.service,
    password_set:true,paused:MOCK.paused});
  if(path==='/api/check'&&method==='POST'){
    MOCK.checking=true;MOCK.window=false;mockLog('外网不通，开始认证...');
    setTimeout(function(){MOCK.checking=false;MOCK.last_check=mockNow();
      MOCK.online=true;MOCK.last_ok=mockNow();MOCK.last_change=mockNow();
      mockLog('认证成功，网络已恢复');},2600);
    return mockJson(MOCK);
  }
  if(path==='/api/login'&&method==='POST'){
    if(MOCK.checking)return mockJson({ok:false,error:'已有检测/认证正在进行，请稍候再试'},400);
    MOCK.manual=true;MOCK.checking=true;MOCK.manual_ok=null;MOCK.manual_msg='正在认证…';
    MOCK.last_manual=mockNow();mockLog('手动认证: 开始 (账号=demo-user 运营商=中国联通)');
    setTimeout(function(){
      MOCK.manual=false;MOCK.checking=false;MOCK.manual_ok=true;MOCK.manual_msg='网络已恢复';
      MOCK.online=true;MOCK.last_check=mockNow();MOCK.last_ok=mockNow();MOCK.last_change=mockNow();
      MOCK.error=null;mockLog('手动认证成功，网络已恢复');},4200);
    return mockJson(Object.assign({ok:true},MOCK));
  }
  if(path==='/api/pause'&&method==='POST'){
    MOCK.paused=!MOCK.paused;mockLog(MOCK.paused?'自动认证已暂停(手动)':'自动认证已恢复(手动)');
    return mockJson(Object.assign({ok:true},MOCK));
  }
  if(path==='/api/clearlog'&&method==='POST'){MOCK_LOGS=['['+mockNow()+'] 日志已清空'];
    return mockJson({ok:true});}
  if(path==='/api/settings'&&method==='POST'){
    try{var b=JSON.parse(opt.body||'{}');if(b.username)MOCK.username=b.username;
      if(b.service)MOCK.service=b.service;
      if(b.window_start)MOCK.window_start=b.window_start;
      if(b.window_end)MOCK.window_end=b.window_end;}catch(e){}
    mockLog('配置已更新: 账号='+MOCK.username+' 运营商='+MOCK.service);
    mockLog('断网窗口已更新: 23:30-05:30 -> '+MOCK.window_start+'-'+MOCK.window_end);
    return mockJson({ok:true,username:MOCK.username,service:MOCK.service,password_set:true,
      paused:MOCK.paused,window_start:MOCK.window_start,window_end:MOCK.window_end});
  }
  return mockJson({error:'not found'},404);
};
</script>
<div style="position:fixed;left:12px;bottom:12px;z-index:99;background:#1a2233;border:1px solid #2a3550;
 color:#8fa0bd;font-size:11px;padding:6px 10px;border-radius:8px;
 font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
 离线预览版（模拟后端）：点【手动认证】可看到完整的状态反馈</div>
"""

assert "</script>" in html
out = html.replace('<script>\nvar _lastState={};', MOCK + '<script>\nvar _lastState={};', 1)
assert MOCK in out, "注入失败"

with open(OUT, "w", encoding="utf-8") as f:
    f.write(out)
print("written", OUT, len(out), "chars")
