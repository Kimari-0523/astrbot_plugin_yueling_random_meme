const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
let categories = [], entries = [], busy = false, maxBytes = 8 * 1024 * 1024;
let reviewRows = [], reviewPage = 1, reviewPages = 1, reviewBusy = false;
const pretty = (n) => `${(n / 1024 / 1024).toFixed(1)} MiB`;
function message(text, error = false) { $("status").textContent = text; $("status").className = error ? "error" : ""; }
function controls() {
  $("category").disabled = busy || !categories.length;
  $("files").disabled = busy;
  $("upload").disabled = busy || !entries.length || !categories.length;
  $("clear").disabled = busy || !entries.length;
  $("refresh").disabled = busy;
  $("create-category").disabled = busy;
  $("category-name").disabled = busy;
  $("category-aliases").disabled = busy;
}
function showAliases() {
  const item = categories.find((x) => x.key === $("category").value);
  $("aliases").textContent = item ? `触发词：${item.aliases.join("、")}${item.enabled ? "" : "。此分类当前禁用，上传不会更改开关。"}` : "";
}
function onlineLabel(item) {
  return item.online_count === null ? "在线图库待载入" : item.online_count ? `在线索引 ${item.online_count} 条` : "仅本地图库";
}
async function refresh(preferred) {
  const value = await bridge.apiGet("gallery");
  const selected = preferred || $("category").value;
  categories = value.categories; maxBytes = value.max_file_bytes;
  $("category").replaceChildren(); $("categories").replaceChildren();
  for (const item of categories) {
    const option = document.createElement("option"); option.value = item.key;
    option.textContent = `${item.title} · ${onlineLabel(item)} · 本地 ${item.count} 张${item.enabled ? "" : "（已禁用）"}`;
    $("category").append(option);
    const card = document.createElement("div"); card.className = "category";
    const title = document.createElement("span"); title.textContent = item.title + (item.enabled ? "" : " · 已禁用");
    const count = document.createElement("strong"); count.textContent = `本地 ${item.count} 张`;
    const online = document.createElement("span"); online.className = "online-count"; online.textContent = onlineLabel(item);
    card.append(title, count, online); $("categories").append(card);
  }
  $("category").value = categories.some((x) => x.key === selected) ? selected : "shigure_ui";
  const onlineTotal = categories.reduce((sum, row) => sum + (row.online_count || 0), 0);
  $("total").textContent = `${categories.length} 个分类 · 本地 ${value.total_count} 张 · 在线索引 ${onlineTotal} 条`;
  showAliases(); controls();
  renderReview();
}
function renderReview() {
  const container = $("review-items"); container.replaceChildren();
  if (!reviewRows.length) { const empty = document.createElement('p'); empty.textContent = '暂无待审核表情。随机收集到的表情会显示在这里。'; container.append(empty); }
  for (const item of reviewRows) {
    const card = document.createElement('article'); card.className = 'review-item';
    const img = document.createElement('img'); img.src = item.thumbnail; img.alt = '待审核表情预览';
    const detail = document.createElement('p'); detail.className = 'hint';
    detail.textContent = `${new Date(item.created*1000).toLocaleString()} · 群 ${item.source.group} · ${item.animated ? '动态图' : '图片'}\n${item.reason}`;
    const original = document.createElement('button'); original.className = 'secondary'; original.textContent = '查看原图 / 动图';
    original.addEventListener('click', async () => { original.disabled = true; try { const r = await bridge.apiGet(`review/${item.id}/image`); img.src = r.data_url; } catch(e) { $("review-status").textContent = e.message; } finally { original.disabled = false; } });
    const label = document.createElement('label'); label.textContent = '入库分类'; label.htmlFor = 'review-category-'+item.id;
    const select = document.createElement('select'); select.id = label.htmlFor; select.setAttribute('aria-label','入库分类 '+item.id.slice(0,8));
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '请选择分类'; select.append(placeholder);
    for(const category of categories) { const option = document.createElement('option'); option.value=category.key; option.textContent=category.title+(category.enabled?'':'（已禁用）'); select.append(option); }
    const actions = document.createElement('div'); actions.className='actions';
    const approve = document.createElement('button'); approve.textContent='通过并入库'; approve.disabled=true;
    const reject = document.createElement('button'); reject.textContent='忽略'; reject.className='secondary';
    select.addEventListener('change',()=>{approve.disabled=!select.value||reviewBusy;});
    async function act(action) {
      if(reviewBusy) return;
      reviewBusy=true; for(const control of container.querySelectorAll('button,select')) control.disabled=true;
      try {
        await bridge.apiPost(`review/${item.id}/${action}`, action==='approve'?{category:select.value}:{});
        $("review-status").textContent = action==='approve'?'已审核入库，立即可用于随机表情。':'已忽略，后续不会重复收集这张图。';
        await refresh(); await refreshReview();
      } catch(e) { $("review-status").textContent=e.message||'操作失败，请刷新后重试。'; }
      finally { reviewBusy=false; renderReview(); }
    }
    approve.addEventListener('click',()=>act('approve')); reject.addEventListener('click',()=>act('reject'));
    actions.append(approve,reject); card.append(img,detail,original,label,select,actions); container.append(card);
  }
  $("review-page").textContent=`${reviewPage} / ${reviewPages}`;
  $("review-prev").disabled=reviewBusy||reviewPage<=1; $("review-next").disabled=reviewBusy||reviewPage>=reviewPages;
}
async function refreshReview() {
  let value=await bridge.apiGet(`review/page/${reviewPage}`);
  if(reviewPage>value.pages) { reviewPage=value.pages; value=await bridge.apiGet(`review/page/${reviewPage}`); }
  reviewRows=value.items; reviewPages=value.pages; $("review-count").textContent=value.total;
  const s=value.settings;
  $("collect-settings").textContent=`自动偷表情：${s.enabled?'已开启':'已关闭'} · 随机概率 ${s.probability}% · 每群尝试间隔 ${s.cooldown} 秒 · ${s.vision?'缺少平台标记时用模型识图，不确定就跳过':'只收集有平台表情标记的图片'}。待审核最多 200 张。`;
  if(value.last_attempt) { const last=value.last_attempt; $("review-status").textContent=`最近采集：${new Date(last.time*1000).toLocaleString()} · ${last.stage} · ${last.reason}`; }
  renderReview();
}
$("review-refresh").addEventListener('click',async()=>{try{await refreshReview();}catch(e){$("review-status").textContent=e.message;}});
$("review-prev").addEventListener('click',async()=>{if(reviewBusy||reviewPage<=1)return;reviewPage--;try{await refreshReview();}catch(e){$("review-status").textContent=e.message;}});
$("review-next").addEventListener('click',async()=>{if(reviewBusy||reviewPage>=reviewPages)return;reviewPage++;try{await refreshReview();}catch(e){$("review-status").textContent=e.message;}});
function clearSelection() {
  for (const row of entries) URL.revokeObjectURL(row.url);
  entries = []; $("files").value = ""; $("queue").replaceChildren();
  $("selected").textContent = "还没有选择图片"; $("progress").hidden = true; controls();
}
$("category").addEventListener("change", showAliases);
$("create-category").addEventListener("click", async () => {
  const title = $("category-name").value.trim();
  if (!title) { $("create-status").textContent = "请填写分类名称。"; return; }
  busy = true; controls(); $("create-status").textContent = "正在创建…";
  try {
    const result = await bridge.apiPost("categories", { title, aliases: $("category-aliases").value });
    clearSelection(); await refresh(result.key);
    $("category-name").value = ""; $("category-aliases").value = "";
    $("create-status").textContent = `已创建「${result.title}」，下方已选中。请上传这个分类的图片。`;
    message(`新分类「${result.title}」已保存；上传图片后即可使用。`);
  } catch (e) { $("create-status").textContent = e.message || "创建失败，请重试。"; }
  finally { busy = false; controls(); }
});
$("clear").addEventListener("click", clearSelection);
function selectFiles(files) {
  for (const row of entries) URL.revokeObjectURL(row.url);
  entries = []; $("queue").replaceChildren(); $("progress").hidden = true;
  if (files.length > 100) { $("files").value = ""; message("一次最多选择 100 张，请分批上传。", true); controls(); return; }
  for (const file of files) {
    const li = document.createElement("li");
    const img = document.createElement("img"); const url = URL.createObjectURL(file); img.src = url; img.alt = "";
    const name = document.createElement("span"); name.className = "file-name"; name.textContent = file.name;
    const outcome = document.createElement("span"); outcome.className = "outcome";
    outcome.textContent = file.size > maxBytes ? "超过 8 MiB" : "待上传";
    li.append(img, name, outcome); $("queue").append(li); entries.push({file, outcome, url, doneKey:null});
  }
  $("selected").textContent = `已选择 ${files.length} 张`;
  message("确认分类后点击上传。重复图片会自动跳过。"); controls();
}
$("files").addEventListener("change", () => selectFiles(Array.from($("files").files)));
$("refresh").addEventListener("click", async () => { try { await refresh(); message("数量已刷新。"); } catch (e) { message(e.message, true); } });
$("upload").addEventListener("click", async () => {
  busy = true; controls(); const key = $("category").value;
  const title = categories.find((x) => x.key === key)?.title || key;
  const progress = $("progress"); progress.hidden = false; progress.max = entries.length; progress.value = 0;
  let added = 0, duplicates = 0, failed = 0;
  for (const row of entries) {
    if (row.doneKey === key) { duplicates++; progress.value++; continue; }
    row.outcome.textContent = "正在上传…"; row.outcome.className = "outcome";
    try {
      if (row.file.size > maxBytes) throw new Error("超过 8 MiB，请先压缩");
      const result = await bridge.upload(`upload/${key}`, row.file);
      row.doneKey = key;
      row.outcome.className = "outcome ok";
      if (result.duplicate) { duplicates++; row.outcome.textContent = "已存在，跳过"; }
      else { added++; row.outcome.textContent = result.frames > 1 ? "已添加 · 动态" : "已添加"; }
    } catch (e) { failed++; row.outcome.className = "outcome error"; row.outcome.textContent = e.message || "上传失败"; }
    progress.value++;
    message(`正在上传到「${title}」：${progress.value} / ${entries.length}`);
  }
  try { await refresh(); } catch (e) { message(`上传已结束，数量刷新失败：${e.message}`, true); }
  busy = false; controls();
  message(`「${title}」上传完成：新增 ${added} 张，重复 ${duplicates} 张，失败 ${failed} 张。成功的图片已立即生效。`, failed > 0);
});
try {
  if (!bridge) throw new Error("请从 AstrBot 后台的插件页面打开此上传入口。");
  await bridge.ready(); await refresh(); await refreshReview();
} catch (e) { message(`无法读取图库：${e.message}`, true); }
window.addEventListener("beforeunload", () => { for (const row of entries) URL.revokeObjectURL(row.url); });
