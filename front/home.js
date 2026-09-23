(() => {
  "use strict";
  const list = document.querySelector("#store-products");
  const state = document.querySelector("#product-state");
  const template = document.querySelector("#store-product-template");
  const search = document.querySelector(".store-search");
  const searchInput = document.querySelector("#store-search-input");

  function stockLabel(product) {
    if (product.quantity === null) return "Остаток уточняется";
    return Number(product.quantity) > 0 ? `В наличии: ${product.quantity}` : "Нет в наличии";
  }
  function card(product) {
    const node = template.content.firstElementChild.cloneNode(true);
    const image = node.querySelector("img");
    const placeholder = node.querySelector(".image-placeholder");
    if (product.image) { image.alt = product.name; image.addEventListener("load", () => { image.hidden = false; placeholder.hidden = true; }); image.addEventListener("error", () => { image.hidden = true; placeholder.hidden = false; }); image.src = product.image; }
    node.querySelector(".store-product-category").textContent = product.category || "Электротехническое оборудование";
    node.querySelector(".store-product-name").textContent = product.name;
    node.querySelector(".store-product-article").textContent = `Артикул: ${product.article}`;
    const stock = node.querySelector(".store-product-stock"); stock.textContent = stockLabel(product); if (product.quantity !== null && Number(product.quantity) <= 0) stock.classList.add("is-empty");
    node.querySelector(".store-product-price").textContent = product.price == null ? "Цена уточняется" : `${product.price} ${product.currency}`;
    node.querySelector(".card-ai-link").addEventListener("click", () => {
      sessionStorage.setItem("ekt-ai-prefill", product.name);
    });
    return node;
  }
  async function loadProducts(query = "") {
    list.replaceChildren(); state.hidden = false; state.textContent = "Загружаем товары…";
    const url = new URL("/api/v1/products", window.location.origin); url.searchParams.set("page", "1"); if (query) url.searchParams.set("q", query);
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.error?.message || "Не удалось получить товары.");
      if (!data.items?.length) { state.textContent = query ? "По вашему запросу товары не найдены." : "Товары пока отсутствуют."; return; }
      list.append(...data.items.slice(0, 8).map(card)); state.hidden = true;
    } catch (error) { state.textContent = `Не удалось загрузить товары: ${error.message}`; }
  }
  search.addEventListener("submit", (event) => { event.preventDefault(); loadProducts(searchInput.value.trim()); });
  document.querySelector("#reload-products").addEventListener("click", () => loadProducts(searchInput.value.trim()));
  loadProducts();
})();
