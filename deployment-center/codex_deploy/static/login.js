(() => {
  "use strict";
  const prefix = document.documentElement.dataset.prefix;
  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch(`${prefix}/api/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: document.getElementById("password").value }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "登录失败");
      location.reload();
    } catch (exc) {
      error.textContent = exc.message;
      button.disabled = false;
    }
  });
})();
