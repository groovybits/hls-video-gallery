#pragma once

#include <string>
#include <string_view>

namespace hls_quality_report {

inline std::string html_escape(std::string_view value) {
    std::string result;
    result.reserve(value.size());
    for (const char character : value) {
        switch (character) {
            case '&': result += "&amp;"; break;
            case '<': result += "&lt;"; break;
            case '>': result += "&gt;"; break;
            case '"': result += "&quot;"; break;
            case '\'': result += "&#39;"; break;
            default: result += character;
        }
    }
    return result;
}

// JSON embedded in an HTML script data block must not be able to terminate that
// block. Escaping these characters is valid in JSON strings and also keeps the
// document safe when a filename or warning contains markup-like text.
inline std::string script_safe_json(std::string_view value) {
    std::string result;
    result.reserve(value.size() + value.size() / 32);
    for (std::size_t index = 0; index < value.size();) {
        const unsigned char character = static_cast<unsigned char>(value[index]);
        if (character == '<') {
            result += "\\u003c";
            ++index;
        } else if (character == '>') {
            result += "\\u003e";
            ++index;
        } else if (character == '&') {
            result += "\\u0026";
            ++index;
        } else if (
            index + 2 < value.size() && character == 0xE2 &&
            static_cast<unsigned char>(value[index + 1]) == 0x80 &&
            (static_cast<unsigned char>(value[index + 2]) == 0xA8 ||
             static_cast<unsigned char>(value[index + 2]) == 0xA9)
        ) {
            result += static_cast<unsigned char>(value[index + 2]) == 0xA8
                ? "\\u2028" : "\\u2029";
            index += 3;
        } else {
            result += value[index++];
        }
    }
    return result;
}

inline std::string json_string(std::string_view value) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string result = "\"";
    result.reserve(value.size() + 2);
    for (const char raw_character : value) {
        const unsigned char character = static_cast<unsigned char>(raw_character);
        switch (character) {
            case '"': result += "\\\""; break;
            case '\\': result += "\\\\"; break;
            case '\b': result += "\\b"; break;
            case '\f': result += "\\f"; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default:
                if (character < 0x20) {
                    result += "\\u00";
                    result += kHex[character >> 4];
                    result += kHex[character & 0x0f];
                } else {
                    result += raw_character;
                }
        }
    }
    result += '"';
    return result;
}

inline std::string render(
    std::string_view report_json,
    std::string_view dashboard_json = {},
    std::string_view fingerprint = {},
    std::string_view title = {}
) {
    std::string html;
    html.reserve(report_json.size() + dashboard_json.size() + 70000);
    html += R"HTML(<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="quality-report-renderer" content="2">
<meta name="quality-report-fingerprint" content=")HTML";
    html += html_escape(fingerprint);
    html += R"HTML(">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; object-src 'none'; form-action 'none'; script-src 'sha256-FeN/izVWLHz7Wxc0kzFGeJ+Meq3K6XeSq/wscwfK6MM='; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; media-src 'none'; font-src 'none'">
<title>Detailed video quality report</title>
<style>
:root{color-scheme:dark;--bg:#0b0710;--panel:#17101d;--panel2:#211327;--line:#55304e;--line2:#362038;--text:#fff6fc;--muted:#c4a9bc;--pink:#ff78c8;--pink2:#ffb4de;--cyan:#52d6e8;--violet:#a98bff;--orange:#ffad69;--green:#77d6a2;--red:#ff718b;--yellow:#f7d66d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#3c1538 0,transparent 37%),linear-gradient(180deg,#100813,var(--bg));color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
button,select{font:inherit}button{cursor:pointer}.shell{width:min(1500px,100%);margin:auto;padding:34px clamp(14px,3vw,42px) 70px}.eyebrow{color:var(--pink);font-size:.72rem;font-weight:900;letter-spacing:.16em;text-transform:uppercase}.hero{display:flex;gap:24px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}.hero h1{font-size:clamp(2rem,5vw,4.4rem);line-height:.96;margin:.15em 0}.subtitle{color:var(--muted);max-width:920px;overflow-wrap:anywhere}.status-pill{border:1px solid var(--line);background:#2a1428;border-radius:999px;padding:8px 12px;color:var(--pink2);white-space:nowrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:20px 0}.card,.panel{background:linear-gradient(145deg,rgba(40,23,45,.96),rgba(19,13,25,.98));border:1px solid var(--line2);box-shadow:0 18px 50px rgba(0,0,0,.25)}.card{border-radius:13px;padding:14px 15px;min-width:0}.card span{display:block;color:var(--muted);font-size:.65rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.card strong{display:block;margin-top:3px;font-size:1.45rem;overflow-wrap:anywhere}.card small{color:var(--muted)}.card.score{border-color:#9d3a78}.card.score strong{color:var(--pink2)}
.note,.warning{margin:14px 0;border-radius:10px;padding:12px 14px}.note{border:1px solid var(--line2);color:var(--muted);background:#140e19}.warning{border-left:4px solid var(--yellow);background:#2a2218}.warning ul{margin:.45em 0 .1em;padding-left:20px}
.panel{border-radius:18px;padding:clamp(14px,2vw,24px);margin-top:24px}.panel-head{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}.panel h2,.panel h3{margin:0}.panel p{color:var(--muted);margin:.35em 0}.range-stats{display:grid;grid-template-columns:repeat(3,minmax(72px,1fr));gap:7px;text-align:center}.range-stats div{background:#130b17;border:1px solid var(--line2);border-radius:9px;padding:8px}.range-stats strong,.range-stats span{display:block}.range-stats span{color:var(--muted);font-size:.65rem;text-transform:uppercase}
.toggles,.controls{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:15px 0}.toggles button,.controls button,.controls select,.pager button{appearance:none;border:1px solid var(--line);border-radius:999px;background:#1b1020;color:var(--muted);padding:7px 11px}.toggles button[aria-pressed="true"]{color:var(--text);border-color:currentColor;background:#2e1930}.toggles .composite{color:var(--pink2)}.toggles .vmaf{color:var(--cyan)}.toggles .phone{color:#76afff}.toggles .ssim{color:var(--green)}.toggles .psnr{color:var(--orange)}.toggles .phash{color:var(--violet)}.toggles .temporal{color:var(--yellow)}.controls button:hover,.toggles button:hover,.pager button:hover{border-color:var(--pink)}.controls .accent{background:#7e285f;color:white;border-color:#c64d95}.controls .range-label{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.plot-wrap{display:grid;grid-template-columns:44px 1fr;gap:8px;margin-top:8px}.y-axis{position:relative;height:360px;color:var(--muted);font-size:.7rem}.y-axis span{position:absolute;right:0;transform:translateY(-50%)}.chart{height:360px;position:relative;border:1px solid var(--line2);border-radius:12px;background:#0c0910;overflow:hidden;outline:none;touch-action:none}.chart:focus{box-shadow:0 0 0 2px var(--pink)}.chart svg{width:100%;height:100%;display:block}.band-excellent{fill:#183225}.band-very-good{fill:#162b2d}.band-good{fill:#24291f}.band-fair{fill:#30241b}.band-poor{fill:#321820}.scene-band{fill:#fff;opacity:.025}.scene-band.odd{opacity:.055}.gridline{stroke:#43303f;stroke-width:1;vector-effect:non-scaling-stroke}.segment-line{stroke:#a08098;stroke-width:1;stroke-dasharray:2 5;opacity:.35;vector-effect:non-scaling-stroke}.series{fill:none;stroke-width:2.1;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}.series-composite{stroke:var(--pink2);stroke-width:3}.series-vmaf{stroke:var(--cyan)}.series-phone{stroke:#76afff}.series-ssimNormalized{stroke:var(--green)}.series-psnrNormalized{stroke:var(--orange)}.series-phash{stroke:var(--violet)}.series-temporalPhash{stroke:var(--yellow)}.cursor{stroke:#fff;stroke-width:1;opacity:.8;vector-effect:non-scaling-stroke}.marker{fill:var(--pink);stroke:#fff;stroke-width:2;vector-effect:non-scaling-stroke}.x-axis{margin-left:52px;display:flex;justify-content:space-between;color:var(--muted);font-size:.72rem;font-variant-numeric:tabular-nums}
.inspector{display:grid;grid-template-columns:minmax(150px,1fr) 4fr;gap:10px;margin-top:15px}.inspect-context{padding:12px;border:1px solid var(--line2);border-radius:11px;background:#110b15}.inspect-context strong,.inspect-context span,.inspect-context small{display:block}.inspect-context strong{font-size:1.25rem;color:var(--pink2)}.inspect-context span,.inspect-context small{color:var(--muted)}.inspect-metrics{display:grid;grid-template-columns:repeat(7,minmax(92px,1fr));gap:7px}.inspect-metrics article{padding:9px;border:1px solid var(--line2);border-radius:9px;background:#110b15}.inspect-metrics span,.inspect-metrics strong,.inspect-metrics small{display:block}.inspect-metrics span{font-size:.62rem;color:var(--muted);text-transform:uppercase}.inspect-metrics strong{font-size:1.03rem}.inspect-metrics small{color:var(--muted);font-size:.67rem}
.tracks{display:grid;gap:8px;margin-top:14px}.track{display:grid;grid-template-columns:145px 1fr;gap:10px;align-items:center}.track-label{color:var(--muted);font-size:.72rem}.track-label strong,.track-label span{display:block}.track-bar{height:22px;position:relative;background:#0e0912;border:1px solid var(--line2);border-radius:7px;overflow:hidden}.track-bar button{position:absolute;top:2px;bottom:2px;min-width:2px;border:0;border-radius:3px;background:#8b4778;opacity:.78}.track-bar button:nth-child(2n){background:#4b8492}.track-bar button:hover,.track-bar button:focus{opacity:1;outline:1px solid #fff}
.drill-grid{display:grid;grid-template-columns:1fr;gap:18px}.details{border:1px solid var(--line2);border-radius:13px;overflow:hidden;background:#110b15}.details summary{cursor:pointer;padding:13px 15px;font-weight:800;background:#251429;color:var(--pink2)}.details summary small{color:var(--muted);font-weight:500;margin-left:7px}.table-tools{display:flex;gap:8px;align-items:center;padding:10px;border-bottom:1px solid var(--line2)}.table-tools input,.table-tools select{border:1px solid var(--line);border-radius:8px;background:#100a14;color:var(--text);padding:7px 9px}.table-tools input{min-width:220px}.table-scroll{max-height:620px;overflow:auto}.table-scroll table{border-collapse:collapse;width:100%;white-space:nowrap;font-variant-numeric:tabular-nums}.table-scroll th,.table-scroll td{padding:9px 10px;border-bottom:1px solid #2c1c2c;text-align:right}.table-scroll th:first-child,.table-scroll td:first-child{text-align:left;position:sticky;left:0;background:#17101d;z-index:2}.table-scroll th{position:sticky;top:0;background:#25182a;color:var(--muted);font-size:.63rem;text-transform:uppercase;letter-spacing:.04em;z-index:3}.table-scroll th:first-child{z-index:4}.table-scroll tr:hover td{background:#241528}.table-scroll button{border:1px solid var(--line);border-radius:6px;background:#271429;color:var(--pink2);padding:4px 7px}.metric-pair{line-height:1.15}.metric-pair span{display:block}.metric-pair small{display:block;color:var(--muted);font-size:.65rem}.empty{padding:20px;color:var(--muted)}footer{margin-top:24px;color:var(--muted);font-size:.75rem}
@media(max-width:800px){.shell{padding-top:22px}.hero,.panel-head{display:block}.status-pill{display:inline-block;margin-top:8px}.inspector{grid-template-columns:1fr}.inspect-metrics{grid-template-columns:repeat(2,1fr)}.plot-wrap{grid-template-columns:32px 1fr}.y-axis,.chart{height:300px}.x-axis{margin-left:40px}.track{grid-template-columns:1fr;gap:4px}.range-stats{margin-top:12px}.controls .range-label{width:100%;margin:0}.table-tools{align-items:stretch;flex-direction:column}.table-tools input{min-width:0;width:100%}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
</style>
</head>
<body>
<main class="shell">
  <header class="hero">
    <div><div class="eyebrow">Objective video analysis</div><h1>Quality report</h1><div class="subtitle" id="subtitle">Loading report…</div></div>
    <div class="status-pill" id="renderer-status">Offline detailed report</div>
  </header>
  <section class="cards" id="summary-cards" aria-label="Quality metric summary"></section>
  <div id="warnings"></div>
  <p class="note">Overall score: 50% Standard VMAF, 20% normalized SSIM, 15% normalized PSNR, and 15% pHash. It combines 70% mean quality with 30% of the weakest decile. Phone VMAF and temporal pHash are diagnostic series.</p>
  <section class="panel" id="explorer">
    <div class="panel-head">
      <div><div class="eyebrow">Frame-level quality</div><h2>Quality explorer</h2><p>Compare each score, reveal scene and HLS boundaries, and inspect any point.</p></div>
      <div class="range-stats" id="range-stats"></div>
    </div>
    <div class="toggles" id="toggles" aria-label="Chart series and overlay visibility"></div>
    <div class="controls">
      <button type="button" id="full">Full video</button><button type="button" id="zoom-in">Zoom in</button><button type="button" id="zoom-out">Zoom out</button><button type="button" class="accent" id="weakest">Weakest scene</button>
      <span class="range-label" id="range-label"></span>
    </div>
    <div class="plot-wrap"><div class="y-axis" id="y-axis"></div><div class="chart" id="chart" tabindex="0" role="application" aria-label="Interactive quality chart"></div></div>
    <div class="x-axis" id="x-axis"></div>
    <div class="inspector"><div class="inspect-context" id="inspect-context"></div><div class="inspect-metrics" id="inspect-metrics"></div></div>
    <div class="tracks" id="tracks"></div>
  </section>
  <section class="panel">
    <div class="panel-head"><div><div class="eyebrow">Interval analysis</div><h2>Scene and HLS drill-down</h2><p>Every interval includes its mean and weakest-decile value for direct comparison.</p></div></div>
    <div class="drill-grid" id="drilldowns"></div>
  </section>
  <footer id="footer"></footer>
</main>
<script id="quality-report-data" type="application/json">{"report":)HTML";
    // The derived dashboard contains the compact chart, scene, segment, video,
    // and summary model. Avoid embedding the enormous full-frame report twice.
    html += dashboard_json.empty() ? script_safe_json(report_json) : "null";
    html += ",\"dashboard\":";
    html += dashboard_json.empty() ? "null" : script_safe_json(dashboard_json);
    html += ",\"title\":";
    html += script_safe_json(json_string(title));
    html += R"HTML(}</script>
<script>
(function(){
"use strict";
const payload=JSON.parse(document.getElementById("quality-report-data").textContent);
const dashboard=payload.dashboard||null,report=payload.report||(dashboard&&dashboard.report_metadata)||{};
const explicitTitle=String(payload.title||"").trim();
const $=id=>document.getElementById(id);
const finite=value=>{if(value===null||value===undefined||value==="")return null;const n=Number(value);return Number.isFinite(n)?n:null};
const clamp=value=>{const n=finite(value);return n===null?null:Math.max(0,Math.min(100,n))};
const first=(object,names)=>{if(!object||typeof object!=="object")return null;for(const name of names){if(Object.prototype.hasOwnProperty.call(object,name)){const n=finite(object[name]);if(n!==null)return n}}return null};
const metricValue=(object,names)=>{if(!object||typeof object!=="object")return null;for(const name of names){const value=object[name];const direct=finite(value);if(direct!==null)return direct;if(value&&typeof value==="object"){for(const field of ["value","score","mean","weighted_mean","average"]){const nested=finite(value[field]);if(nested!==null)return nested}}}return null};
const durationText=seconds=>{const total=Math.max(0,Math.round(finite(seconds)||0)),h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=total%60;return(h?h+":":"")+(h?String(m).padStart(2,"0"):m)+":"+String(s).padStart(2,"0")};
const numberText=(value,digits=1,suffix="")=>{const n=finite(value);return n===null?"—":n.toFixed(digits)+suffix};
const bytesText=value=>{let n=finite(value);if(n===null||n<0)return"—";const units=["B","KB","MB","GB"];let i=0;while(n>=1024&&i<units.length-1){n/=1024;i++}return n.toFixed(i?1:0)+" "+units[i]};
const band=value=>{const n=finite(value);return n===null?"Unrated":n>=90?"Excellent":n>=80?"Very good":n>=70?"Good":n>=55?"Fair":"Poor"};
const make=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node};
const append=(parent,...nodes)=>{for(const node of nodes)if(node)parent.appendChild(node);return parent};
const aliases={
 composite:["composite","score","overall_score"],vmaf:["vmaf_standard","vmaf","standard_vmaf"],phone:["vmaf_phone","phone_vmaf","vmaf_mobile"],
 ssim:["ssim","ssim_y"],ssimNormalized:["ssim_normalized","normalized_ssim","ssim_score"],psnr:["psnr_y","psnr"],
 psnrNormalized:["psnr_normalized","normalized_psnr","psnr_score"],phash:["phash_similarity","phash"],temporalPhash:["temporal_consistency","temporal_phash"]
};
const series=[
 {key:"composite",label:"Overall",className:"composite"},{key:"vmaf",label:"Standard VMAF",className:"vmaf"},
 {key:"phone",label:"Phone VMAF",className:"phone"},{key:"ssimNormalized",label:"SSIM score",className:"ssim"},
 {key:"psnrNormalized",label:"PSNR score",className:"psnr"},{key:"phash",label:"pHash",className:"phash"},
 {key:"temporalPhash",label:"Temporal pHash",className:"temporal"}
];
function pointFrom(source,index){
 const nested=source&&source.metrics||{},raw=name=>metricValue(nested,aliases[name])??metricValue(source,aliases[name]);
 const ssim=raw("ssim"),psnr=raw("psnr");
 const result={time:Math.max(0,first(source,["time_seconds","timestamp_seconds","time","pts_time"])??index),frame:first(source,["frame"]),scene:first(source,["scene_index","scene"]),segment:first(source,["segment_index"]),ssim,psnr};
 result.vmaf=clamp(raw("vmaf"));result.phone=clamp(raw("phone"));result.ssimNormalized=clamp(raw("ssimNormalized")??(ssim===null?null:ssim*100));
 result.psnrNormalized=clamp(raw("psnrNormalized")??(psnr===null?null:(psnr-20)/30*100));result.phash=clamp(raw("phash"));result.temporalPhash=clamp(raw("temporalPhash"));result.composite=clamp(raw("composite"));
 if(result.composite===null&&[result.vmaf,result.ssimNormalized,result.psnrNormalized,result.phash].every(v=>v!==null))result.composite=clamp(.5*result.vmaf+.2*result.ssimNormalized+.15*result.psnrNormalized+.15*result.phash);
 return result;
}
const pointSource=dashboard&&dashboard.overview&&Array.isArray(dashboard.overview.points)&&dashboard.overview.points.length?dashboard.overview.points:(Array.isArray(report.frames)&&report.frames.length?report.frames:(Array.isArray(report.timeline)?report.timeline:[]));
const points=pointSource.map(pointFrom).filter(point=>series.some(item=>point[item.key]!==null)).sort((a,b)=>a.time-b.time);
function metricSummary(source,key){
 const metrics=source&&source.metrics||source||{},names=aliases[key]||[key];let object=null;
 for(const name of names)if(metrics[name]&&typeof metrics[name]==="object"){object=metrics[name];break}
 if(!object)return{mean:metricValue(metrics,names),worst:null};
 return{mean:first(object,["mean","weighted_mean","average","score"]),worst:first(object,["worst_decile","worstDecile","low_10"])};
}
function summarize(selected,key){
 const values=selected.map(point=>finite(point[key])).filter(value=>value!==null).sort((a,b)=>a-b);
 if(!values.length)return{mean:null,worst:null};const count=Math.max(1,Math.ceil(values.length*.1));
 return{mean:values.reduce((sum,value)=>sum+value,0)/values.length,worst:values.slice(0,count).reduce((sum,value)=>sum+value,0)/count};
}
function rangeFrom(source,index,kind){
 const start=Math.max(0,first(source,["start_seconds","start"])??0),end=first(source,["end_seconds","end"])??(start+(first(source,["duration_seconds","duration"])||0));
 const result={source,index:first(source,["index"])??index+(kind==="segment"?0:1),displayIndex:index+1,start,end:Math.max(start,end),duration:Math.max(0,end-start),score:first(source,["score"]),band:source.band||null,uri:source.uri||null,size:first(source,["size_bytes"]),bitrate:first(source,["bitrate_bps"]),sequence:first(source,["sequence"]),kind,metrics:{}};
 for(const item of series)result.metrics[item.key]=metricSummary(source,item.key);
 result.metrics.ssimRaw=metricSummary(source,"ssim");result.metrics.psnrRaw=metricSummary(source,"psnr");
 if(result.score===null){const selected=points.filter(point=>point.time>=start&&point.time<(end||start+.001));for(const item of series)if(result.metrics[item.key].mean===null)result.metrics[item.key]=summarize(selected,item.key);result.metrics.ssimRaw=summarize(selected,"ssim");result.metrics.psnrRaw=summarize(selected,"psnr");const composite=result.metrics.composite;result.score=composite.mean===null?null:.7*composite.mean+.3*(composite.worst??composite.mean)}
 result.band=result.band||band(result.score);return result;
}
const sceneSource=dashboard&&Array.isArray(dashboard.scenes)?dashboard.scenes:(Array.isArray(report.scenes)?report.scenes:[]);
let segmentSource=dashboard&&Array.isArray(dashboard.hls_segments)?dashboard.hls_segments:[];
const hasExactSegments=segmentSource.length>0;
const scenes=sceneSource.map((value,index)=>rangeFrom(value,index,"scene")).filter(value=>value.end>value.start).sort((a,b)=>a.start-b.start);
let segments=segmentSource.map((value,index)=>rangeFrom(value,index,"segment")).filter(value=>value.end>value.start).sort((a,b)=>a.start-b.start);
const video=dashboard&&dashboard.video||report.video||{};
const duration=Math.max(first(video,["duration_seconds"])||0,points.length?points[points.length-1].time:0,scenes.length?scenes[scenes.length-1].end:0,segments.length?segments[segments.length-1].end:0,1);
if(!segments.length&&duration>0){segmentSource=[];for(let start=0,index=0;start<duration;start+=6,index++)segmentSource.push({index,sequence:index,start_seconds:start,end_seconds:Math.min(duration,start+6),duration_seconds:Math.min(6,duration-start),exact:false});segments=segmentSource.map((value,index)=>rangeFrom(value,index,"segment"))}
const summary=dashboard&&dashboard.summary||report.summary||{},overallMetrics=report.metrics||{};
const overallScore=first(summary,["score"])??metricSummary(overallMetrics,"composite").mean;
const inputs=report.inputs||{},reference=inputs.reference||"reference video",distorted=inputs.distorted||"encoded video";
$("subtitle").textContent=(explicitTitle?explicitTitle:distorted+" compared with "+reference)+" · "+durationText(duration)+" · "+(first(video,["frames_analyzed"])||points.length).toLocaleString()+" analyzed frames";
document.title=(explicitTitle||distorted)+" — detailed quality report";
$("renderer-status").textContent=segments.length.toLocaleString()+(hasExactSegments?" exact HLS segments":" nominal 6-second segments");
function addCard(label,value,detail,className){
 const card=make("article","card"+(className?" "+className:""));append(card,make("span","",label),make("strong","",value),make("small","",detail||""));$("summary-cards").appendChild(card);
}
addCard("Overall",numberText(overallScore,1),band(overallScore),"score");
const summaryFields=[
 ["Standard VMAF","vmaf",""],["Phone VMAF","phone","diagnostic"],["SSIM score","ssimNormalized","normalized"],
 ["PSNR score","psnrNormalized","normalized"],["pHash","phash","perceptual"],["Temporal pHash","temporalPhash","diagnostic"]
];
for(const [label,key,detail] of summaryFields){const direct=metricValue(summary,aliases[key]);const metric=metricSummary(overallMetrics,key);addCard(label,numberText(direct??metric.mean,1),detail)}
addCard("Raw SSIM",numberText(metricValue(summary,aliases.ssim)??metricSummary(overallMetrics,"ssim").mean,5),"source scale");
addCard("Raw PSNR",numberText(metricValue(summary,aliases.psnr)??metricSummary(overallMetrics,"psnr").mean,2," dB"),"source scale");
if(Array.isArray(report.warnings)&&report.warnings.length){const warning=make("div","warning");append(warning,make("strong","","Warnings"));const list=make("ul");for(const value of report.warnings)list.appendChild(make("li","",value));warning.appendChild(list);$("warnings").appendChild(warning)}
if(report.preprocessing&&report.preprocessing.reference_deinterlace){$("warnings").appendChild(make("div","note","reference deinterlaced with yadif=deint=interlaced before frame alignment"))}
const state={start:0,end:duration,selected:points.length?points[0].time:0,label:"Full video",showScenes:true,showSegments:true,visible:{}};
for(const item of series)state.visible[item.key]=true;
const toggles=$("toggles");
for(const item of series){const button=make("button",item.className,item.label);button.type="button";button.setAttribute("aria-pressed","true");button.onclick=()=>{state.visible[item.key]=!state.visible[item.key];button.setAttribute("aria-pressed",String(state.visible[item.key]));draw()};toggles.appendChild(button)}
for(const [label,key] of [["Scenes","showScenes"],["HLS segments","showSegments"]]){const button=make("button","",label);button.type="button";button.setAttribute("aria-pressed","true");button.onclick=()=>{state[key]=!state[key];button.setAttribute("aria-pressed",String(state[key]));draw()};toggles.appendChild(button)}
for(const value of [100,75,50,25,0]){const label=make("span","",value);label.style.top=(100-value)+"%";$("y-axis").appendChild(label)}
const svgNS="http://www.w3.org/2000/svg",svgNode=(name,attributes)=>{const node=document.createElementNS(svgNS,name);for(const [key,value] of Object.entries(attributes||{}))node.setAttribute(key,String(value));return node};
const chart=$("chart"),x=time=>(time-state.start)/Math.max(.001,state.end-state.start)*1000,y=score=>360-clamp(score??0)/100*360;
const inView=ranges=>ranges.filter(range=>range.end>state.start&&range.start<state.end);
function nearest(time){let low=0,high=points.length;while(low<high){const middle=(low+high)>>1;if(points[middle].time<time)low=middle+1;else high=middle}if(low<=0)return 0;if(low>=points.length)return points.length-1;return time-points[low-1].time<=points[low].time-time?low-1:low}
function downsample(values,limit){
 if(values.length<=limit)return values;const result=[values[0]],buckets=Math.max(1,Math.floor((limit-2)/(series.length*2)));
 for(let bucket=0;bucket<buckets;bucket++){const start=1+Math.floor(bucket*(values.length-2)/buckets),end=1+Math.floor((bucket+1)*(values.length-2)/buckets),selected=new Set();for(const item of series){if(!state.visible[item.key])continue;let min=-1,max=-1;for(let i=start;i<end;i++){if(values[i][item.key]===null)continue;if(min<0||values[i][item.key]<values[min][item.key])min=i;if(max<0||values[i][item.key]>values[max][item.key])max=i}if(min>=0)selected.add(min);if(max>=0)selected.add(max)}for(const index of [...selected].sort((a,b)=>a-b))result.push(values[index])}result.push(values[values.length-1]);return result.length>limit?result.filter((_,index)=>index===0||index===result.length-1||index%Math.ceil(result.length/limit)===0).slice(0,limit-1).concat(result[result.length-1]):result;
}
function pointReadout(point,item){const value=point&&point[item.key];if(value===null||value===undefined)return"—";return numberText(value,1)}
function updateInspector(){
 if(!points.length)return;const point=points[nearest(state.selected)],scene=scenes.find(value=>point.time>=value.start&&point.time<value.end),segment=segments.find(value=>point.time>=value.start&&point.time<value.end);
 const context=$("inspect-context");context.replaceChildren();append(context,make("strong","",durationText(point.time)),make("span","",(scene?"Scene "+scene.displayIndex:"No scene")+" · "+(segment?"HLS "+segment.displayIndex:"No segment")),make("small","","Frame "+(point.frame===null?"—":Math.round(point.frame).toLocaleString())+" · arrows inspect · Shift/Control + wheel zoom"));
 const metrics=$("inspect-metrics");metrics.replaceChildren();for(const item of series){const card=make("article","");append(card,make("span","",item.label),make("strong","",pointReadout(point,item)));if(item.key==="ssimNormalized")card.appendChild(make("small","","raw "+numberText(point.ssim,6)));if(item.key==="psnrNormalized")card.appendChild(make("small","","raw "+numberText(point.psnr,2," dB")));metrics.appendChild(card)}
 chart.setAttribute("aria-label","Selected "+durationText(point.time)+", overall "+numberText(point.composite,1)+". Use arrow keys to inspect.");
}
function track(title,ranges){
 if(!ranges.length)return null;const row=make("div","track"),label=make("div","track-label"),bar=make("div","track-bar"),visible=inView(ranges);append(label,make("strong","",title),make("span","",visible.length.toLocaleString()+" in view"));
 const step=Math.max(1,Math.ceil(visible.length/400));visible.forEach((range,index)=>{if(index%step&&index+1!==visible.length)return;const button=make("button","");button.type="button";const left=Math.max(state.start,range.start),right=Math.min(state.end,range.end);button.style.left=Math.max(0,x(left)/10)+"%";button.style.width=Math.max(.15,(x(right)-x(left))/10)+"%";button.title=(range.kind==="scene"?"Scene ":"HLS ")+range.displayIndex+" · "+durationText(range.start)+"–"+durationText(range.end)+" · "+numberText(range.score,1);button.onclick=()=>setView(range.start,range.end,(range.kind==="scene"?"Scene ":"HLS ")+range.displayIndex);bar.appendChild(button)});return append(row,label,bar);
}
function draw(){
 const svg=svgNode("svg",{viewBox:"0 0 1000 360",preserveAspectRatio:"none","aria-hidden":"true"});
 for(const [low,high,name] of [[90,100,"excellent"],[80,90,"very-good"],[70,80,"good"],[55,70,"fair"],[0,55,"poor"]])svg.appendChild(svgNode("rect",{x:0,y:y(high),width:1000,height:y(low)-y(high),class:"band-"+name}));
 if(state.showScenes)inView(scenes).forEach((range,index)=>svg.appendChild(svgNode("rect",{x:x(Math.max(state.start,range.start)),y:0,width:Math.max(.2,x(Math.min(state.end,range.end))-x(Math.max(state.start,range.start))),height:360,class:"scene-band "+(index%2?"odd":"even")})));
 for(const value of [0,25,50,75,100])svg.appendChild(svgNode("line",{x1:0,y1:y(value),x2:1000,y2:y(value),class:"gridline"}));
 if(state.showSegments){const visible=inView(segments),step=Math.max(1,Math.ceil(visible.length/600));visible.forEach((range,index)=>{if(index%step===0)svg.appendChild(svgNode("line",{x1:x(range.start),y1:0,x2:x(range.start),y2:360,class:"segment-line"}))})}
 const visiblePoints=points.filter(point=>point.time>=state.start&&point.time<=state.end),sampled=downsample(visiblePoints,1000);
 for(const item of series){if(!state.visible[item.key])continue;let run=[];const flush=()=>{if(run.length>1)svg.appendChild(svgNode("polyline",{points:run.join(" "),class:"series series-"+item.key}));run=[]};for(const point of sampled){const value=point[item.key];if(value===null){flush();continue}run.push(x(point.time).toFixed(2)+","+y(value).toFixed(2))}flush()}
 if(points.length){const point=points[nearest(state.selected)],cursorX=x(point.time);svg.appendChild(svgNode("line",{x1:cursorX,y1:0,x2:cursorX,y2:360,class:"cursor"}));svg.appendChild(svgNode("circle",{cx:cursorX,cy:y(point.composite),r:5,class:"marker"}))}
 chart.replaceChildren(svg);const axis=$("x-axis");axis.replaceChildren();for(let tick=0;tick<5;tick++)axis.appendChild(make("span","",durationText(state.start+(state.end-state.start)*tick/4)));
 $("range-label").textContent=durationText(state.start)+"–"+durationText(state.end)+" · "+state.label;const stats=$("range-stats");stats.replaceChildren();for(const [value,label] of [[visiblePoints.length,"samples"],[inView(scenes).length,"scenes"],[inView(segments).length,"segments"]]){const card=make("div","");append(card,make("strong","",Number(value).toLocaleString()),make("span","",label));stats.appendChild(card)}
 const tracks=$("tracks");tracks.replaceChildren();if(state.showScenes)append(tracks,track("Scenes",scenes));if(state.showSegments)append(tracks,track(hasExactSegments?"Exact HLS segments":"Nominal 6-second segments",segments));updateInspector();
}
function setView(start,end,label){
 const span=Math.max(.25,Math.min(duration,(finite(end)||duration)-(finite(start)||0))),nextStart=Math.max(0,Math.min(duration-span,finite(start)||0));state.start=nextStart;state.end=Math.min(duration,nextStart+span);state.label=label||"Focused range";state.selected=Math.max(state.start,Math.min(state.end,state.selected));draw();chart.focus({preventScroll:true});
}
function zoom(factor,center=state.selected){const span=Math.max(.5,Math.min(duration,(state.end-state.start)*factor)),ratio=(center-state.start)/Math.max(.001,state.end-state.start),start=Math.max(0,Math.min(duration-span,center-span*Math.max(0,Math.min(1,ratio))));setView(start,start+span,factor<1?"Zoomed in":"Zoomed out")}
$("full").onclick=()=>setView(0,duration,"Full video");$("zoom-in").onclick=()=>zoom(.5);$("zoom-out").onclick=()=>zoom(2);$("weakest").onclick=()=>{const weakest=scenes.filter(scene=>scene.score!==null).sort((a,b)=>a.score-b.score)[0];if(weakest)setView(weakest.start,weakest.end,"Weakest scene "+weakest.displayIndex)};
const eventTime=event=>{const bounds=chart.getBoundingClientRect();return state.start+Math.max(0,Math.min(1,(event.clientX-bounds.left)/Math.max(1,bounds.width)))*(state.end-state.start)};
chart.addEventListener("pointermove",event=>{if(points.length){state.selected=points[nearest(eventTime(event))].time;draw()}});
chart.addEventListener("click",event=>{if(points.length){state.selected=points[nearest(eventTime(event))].time;draw()}});
chart.addEventListener("wheel",event=>{if(!event.ctrlKey&&!event.shiftKey)return;event.preventDefault();zoom(event.deltaY>0?1.5:.67,eventTime(event))},{passive:false});
chart.addEventListener("keydown",event=>{if(!points.length)return;let index=nearest(state.selected);if(event.key==="ArrowLeft"||event.key==="ArrowRight"||event.key==="Home"||event.key==="End"){event.preventDefault();if(event.key==="Home")index=nearest(state.start);else if(event.key==="End")index=nearest(state.end);else index=Math.max(0,Math.min(points.length-1,index+(event.key==="ArrowRight"?1:-1)));state.selected=points[index].time;draw()}else if(event.key==="PageUp"||event.key==="PageDown"){event.preventDefault();const span=state.end-state.start,shift=span*.75*(event.key==="PageDown"?1:-1),start=Math.max(0,Math.min(duration-span,state.start+shift));setView(start,start+span,"Panned range")}});
function pair(range,key,digits=1,suffix=""){const value=range.metrics[key]||{mean:null,worst:null},wrap=make("div","metric-pair");append(wrap,make("span","",numberText(value.mean,digits,suffix)),make("small","","low "+numberText(value.worst,digits,suffix)));return wrap}
function drilldown(title,ranges,kind){
 const details=make("details","details");details.open=kind==="scene";const summaryNode=make("summary","",title);summaryNode.appendChild(make("small","",ranges.length.toLocaleString()+" total"));details.appendChild(summaryNode);
 if(!ranges.length){details.appendChild(make("div","empty",kind==="segment"?"No segment intervals were reported.":"No scene intervals were reported."));return details}
 const tools=make("div","table-tools"),search=make("input",""),sort=make("select","");search.type="search";search.placeholder="Filter by number, range, URI…";for(const [value,label] of [["timeline","Timeline order"],["weakest","Weakest first"]]){const option=make("option","",label);option.value=value;sort.appendChild(option)}append(tools,search,sort);details.appendChild(tools);
 const scroll=make("div","table-scroll"),table=make("table",""),head=make("thead",""),header=make("tr","");
 const columns=["Interval","Range","Score","Overall","VMAF","Phone","SSIM raw","SSIM score","PSNR raw","PSNR score","pHash","Temporal","Bytes / bitrate","Focus"];
 for(const label of columns)header.appendChild(make("th","",label));head.appendChild(header);const body=make("tbody","");append(table,head,body);scroll.appendChild(table);details.appendChild(scroll);
 function renderRows(){const query=search.value.trim().toLowerCase();let values=ranges.filter(range=>!query||[(kind==="scene"?"scene ":"hls ")+range.displayIndex,durationText(range.start)+"-"+durationText(range.end),range.uri||""].join(" ").toLowerCase().includes(query));if(sort.value==="weakest")values=values.slice().sort((a,b)=>(a.score??Infinity)-(b.score??Infinity));body.replaceChildren();for(const range of values){const row=make("tr","");let label=(kind==="scene"?"Scene ":"HLS ")+range.displayIndex;if(kind==="segment"&&range.sequence!==null)label+=" · seq "+range.sequence;append(row,make("td","",label),make("td","",durationText(range.start)+"–"+durationText(range.end)),make("td","",numberText(range.score,1)+" · "+range.band));for(const [key,digits,suffix] of [["composite",1,""],["vmaf",1,""],["phone",1,""],["ssimRaw",5,""],["ssimNormalized",1,""],["psnrRaw",2," dB"],["psnrNormalized",1,""],["phash",1,""],["temporalPhash",1,""]]){const cell=make("td","");cell.appendChild(pair(range,key,digits,suffix));row.appendChild(cell)}const data=make("td","",kind==="segment"?bytesText(range.size)+" · "+(range.bitrate===null?"—":numberText(range.bitrate/1000,0," kb/s")):(range.source.frame_count?Number(range.source.frame_count).toLocaleString()+" frames":"—"));const action=make("td",""),button=make("button","","Focus");button.type="button";button.onclick=()=>{setView(range.start,range.end,label);$("explorer").scrollIntoView({behavior:"smooth",block:"start"})};action.appendChild(button);append(row,data,action);body.appendChild(row)}}search.oninput=renderRows;sort.onchange=renderRows;renderRows();return details;
}
append($("drilldowns"),drilldown("Scene details",scenes,"scene"),drilldown(hasExactSegments?"Exact HLS segment details":"Nominal HLS segment details",segments,"segment"));
const source=dashboard&&dashboard.source||{};$("footer").textContent="Generated by "+((report.analyzer&&report.analyzer.name)||"hls-quality-analyzer")+" "+((report.analyzer&&report.analyzer.version)||report.analyzer_version||source.analyzer_version||"")+" · standalone renderer 2 · "+(report.generated_at||source.report_generated_at||dashboard&&dashboard.generated_at||"");
if(!points.length){$("explorer").appendChild(make("div","warning","No frame-level timeline is present in this report."));chart.removeAttribute("tabindex")}else draw();
})();
</script>
</body>
</html>
)HTML";
    return html;
}

}  // namespace hls_quality_report
