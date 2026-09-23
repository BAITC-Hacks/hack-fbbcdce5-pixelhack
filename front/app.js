(() => {
  "use strict";
  const apiRoot = "/api/v1";
  const sessionKey = "ekt-ai-session";
  const elements = {
    messages: document.querySelector("#messages"), form: document.querySelector("#chat-form"), input: document.querySelector("#chat-input"), send: document.querySelector("#send-button"), file: document.querySelector("#file-input"), preview: document.querySelector("#attachment-preview"), status: document.querySelector("#connection-status"), count: document.querySelector("#cart-count"), template: document.querySelector("#product-template")
  };
  let session;
  let proposalSection;

  let proposalId;
  function readSession() { try { return JSON.parse(sessionStorage.getItem(sessionKey)); } catch { return null; } }
  function saveSession(value) { session = value; sessionStorage.setItem(sessionKey, JSON.stringify(value)); }
  function clearSession() { session = null; sessionStorage.removeItem(sessionKey); }
  function setStatus(message, kind = "") { elements.status.textContent = message; elements.status.className = `status ${kind}`; }
  function authHeaders(extra = {}) { return { Authorization: `Bearer ${session.access_token}`, ...extra }; }
  function errorMessage(body, fallback) { return body?.error?.message || fallback; }
  async function request(path, options = {}) {
    const response = await fetch(`${apiRoot}${path}`, options);
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) { if (response.status === 401) clearSession(); throw new Error(errorMessage(body, `Ошибка сервера (${response.status})`)); }
    return body;
  }
  async function ensureSession() { const stored = readSession(); if (stored?.session_id && stored?.access_token) { session = stored; return; } saveSession(await request("/sessions", { method: "POST" })); }
  function addMessage(text, role = "assistant") {
    const article = document.createElement("article"); article.className = `message ${role}-message`;
    const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = role === "user" ? "Вы" : "AI";
    const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = text;
    article.append(avatar, bubble); elements.messages.append(article); article.scrollIntoView({ block: "end", behavior: "smooth" }); return article;
  }
  function setCart(cart) { elements.count.textContent = String(cart?.items?.reduce((sum, item) => sum + Number(item.quantity), 0) || 0); }
  function money(value, currency = "KZT") { return value == null ? "Цена не указана" : `${value} ${currency}`; }
  function stock(product) { if (product.quantity === null) return ["Остаток уточняется", ""]; return Number(product.quantity) > 0 ? [`В наличии: ${product.quantity}`, ""] : ["Нет в наличии", "out"]; }
  function productCard(product, reason = "") {
    const node = elements.template.content.firstElementChild.cloneNode(true); const [stockText, stockClass] = stock(product);
    const image = node.querySelector(".product-image img"); const placeholder = node.querySelector(".product-image-placeholder");
    if (product.image) {
      image.alt = product.name;
      image.addEventListener("load", () => { image.hidden = false; placeholder.hidden = true; }, { once: true });
      image.addEventListener("error", () => { image.hidden = true; placeholder.hidden = false; }, { once: true });
      image.src = product.image;
    }
    node.querySelector(".product-category").textContent = product.category || "Категория не указана";
    const stockNode = node.querySelector(".stock"); stockNode.textContent = stockText; if (stockClass) stockNode.classList.add(stockClass);
    node.querySelector(".product-name").textContent = product.name; node.querySelector(".article").textContent = `Артикул: ${product.article}`; node.querySelector(".price").textContent = money(product.price, product.currency);
    const specs = node.querySelector(".specs"); for (const [key, value] of Object.entries(product.properties || {})) { const dt = document.createElement("dt"); dt.textContent = key; const dd = document.createElement("dd"); dd.textContent = Array.isArray(value) ? value.join(", ") : value; specs.append(dt, dd); }
    if (!Object.keys(product.properties || {}).length) specs.remove();
    if (product.certificates?.length) { const certificate = node.querySelector(".certificate"); certificate.href = product.certificates[0]; certificate.hidden = false; }
    if (reason) { const reasonNode = node.querySelector(".analog-reason"); reasonNode.textContent = `Почему аналог: ${reason}`; reasonNode.hidden = false; }
    const prepare = node.querySelector(".prepare-button"); prepare.addEventListener("click", () => createProposal(product.id, node.querySelector(".quantity-input").value, prepare)); return node;
  }
  function renderProducts(products, alternatives) { const cards = [...products.map((product) => productCard(product)), ...alternatives.map(({ product, reason }) => productCard(product, reason))]; if (!cards.length) return; const container = document.createElement("section"); container.className = "results"; container.setAttribute("aria-label", "Товары"); container.append(...cards); elements.messages.append(container); }
  function renderProposal(action) {
    if (proposalSection && proposalId === action?.id) return;
    proposalSection?.remove(); proposalSection = null; proposalId = null;
    if (!action) return;
    const section = document.createElement("section"); section.className = "proposal";
    const text = document.createElement("p"); text.textContent = `Подтвердите добавление: ${action.product.name}, ${action.quantity} шт., ${money(action.product.price, action.product.currency)} за единицу.`;
    const note = document.createElement("p"); note.textContent = "До нажатия подтверждения корзина не изменится.";
    const button = document.createElement("button"); button.type = "button"; button.className = "confirm-button"; button.textContent = "Подтвердить добавление";
    button.addEventListener("click", async () => { button.disabled = true; try { const cart = await request(`/sessions/${session.session_id}/cart/proposals/${action.id}/confirm`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ confirmed: true }) }); setCart(cart); addMessage("Товар добавлен в локальную корзину. Заказ на ekt.kz не создан."); renderProposal(null); } catch (error) { addMessage(error.message); button.disabled = false; } });
    section.append(text, note, button); elements.messages.append(section); proposalSection = section; proposalId = action.id;
  }
  function renderResponse(response) { addMessage(response.message); renderProducts(response.products || [], response.alternatives || []); renderProposal(response.pending_action); setCart(response.cart); }
  async function createProposal(productId, quantity, button) { button.disabled = true; try { renderProposal(await request(`/sessions/${session.session_id}/cart/proposals`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ product_id: productId, quantity }) })); } catch (error) { addMessage(error.message); } finally { button.disabled = false; } }
  function selectedFileType(file) { const byExtension = { pdf: "application/pdf", jpg: "image/jpeg", jpeg: "image/jpeg", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }; return byExtension[file.name.split(".").pop().toLowerCase()] || file.type; }
  async function sendAttachment(file) { if (file.size > 5 * 1024 * 1024) throw new Error("Максимальный размер файла — 5 МиБ."); const type = selectedFileType(file); if (!type) throw new Error("Поддерживаются PDF, JPEG, DOCX и XLSX."); return request(`/sessions/${session.session_id}/chat/attachment`, { method: "POST", headers: authHeaders({ "Content-Type": type }), body: file }); }
  async function submit(event) {
    event.preventDefault(); const text = elements.input.value.trim(); const file = elements.file.files[0]; if (!text && !file) return;
    if (text) { addMessage(text, "user"); elements.input.value = ""; } elements.send.disabled = true; setStatus("Ассистент отвечает…");
    try { if (!session) await ensureSession(); let response; if (text) response = await request(`/sessions/${session.session_id}/chat`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ message: text }) }); if (file) { addMessage(`Файл: ${file.name}`, "user"); response = await sendAttachment(file); elements.file.value = ""; elements.preview.hidden = true; } if (response) renderResponse(response); setStatus("Подключено", "online"); }
    catch (error) { addMessage(error.message); setStatus(session ? "Ошибка запроса" : "Сессия истекла — повторите запрос"); } finally { elements.send.disabled = false; }
  }
  async function init() { try { await ensureSession(); await request("/health"); let cart; try { cart = await request(`/sessions/${session.session_id}/cart`, { headers: authHeaders() }); } catch (error) { if (session) throw error; await ensureSession(); cart = await request(`/sessions/${session.session_id}/cart`, { headers: authHeaders() }); } setCart(cart); setStatus("Подключено", "online"); } catch (error) { clearSession(); setStatus("Сервис недоступен"); addMessage(error.message); } }
  elements.form.addEventListener("submit", submit); elements.file.addEventListener("change", () => { const file = elements.file.files[0]; elements.preview.hidden = !file; elements.preview.textContent = file ? `Выбран файл: ${file.name}` : ""; }); document.querySelectorAll(".suggestion").forEach((button) => button.addEventListener("click", () => { elements.input.value = button.textContent; elements.input.focus(); })); init();
  try {
    const productName = sessionStorage.getItem("ekt-ai-prefill");
    if (productName) {
      sessionStorage.removeItem("ekt-ai-prefill");
      elements.input.value = productName;
      elements.input.focus();
    }
  } catch { /* Session storage is optional; the chat remains usable without it. */ }
})();
