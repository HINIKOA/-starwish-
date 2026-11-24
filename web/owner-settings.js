let tokenPrompt;

function askForOwnerToken() {
  if (tokenPrompt) return tokenPrompt;
  const dialog = document.createElement("dialog");
  dialog.className = "owner-settings-dialog";
  dialog.setAttribute("aria-labelledby", "owner-settings-title");
  dialog.innerHTML = `<form method="dialog">
    <h2 id="owner-settings-title">验证主人身份</h2>
    <p>修改受保护的设置需要当前主人令牌。令牌不会保存。</p>
    <label for="owner-settings-token">当前主人令牌</label>
    <input id="owner-settings-token" type="password" autocomplete="off" required autofocus>
    <div class="row"><button type="button" data-cancel>取消</button><button class="primary">验证并保存</button></div>
  </form>`;
  document.body.append(dialog);
  const input = dialog.querySelector("input");
  dialog.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.querySelector("form").onsubmit = (event) => {
    event.preventDefault();
    dialog.close("verify");
  };
  tokenPrompt = new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      const token = dialog.returnValue === "verify" ? input.value : "";
      input.value = "";
      dialog.remove();
      tokenPrompt = null;
      resolve(token);
    }, { once: true });
  });
  dialog.showModal();
  return tokenPrompt;
}

export async function saveProtectedSettings(api, patch) {
  const options = { method: "PUT", body: patch };
  try {
    return await api("/api/settings", options);
  } catch (error) {
    if (error.status !== 403) throw error;
    const token = await askForOwnerToken();
    if (!token) throw new Error("未验证主人身份，设置没有修改。");
    return api("/api/settings", { ...options, headers: { "X-StarWish-Owner": token } });
  }
}
