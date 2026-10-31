// Progressive enhancement only: every page works without JavaScript,
// and the server re-checks every rule shown here.
(function () {
  // Mobile menu
  var toggle = document.querySelector(".menu-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var shell = document.querySelector(".shell");
      var open = shell.classList.toggle("menu-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Live password rules (same rules as core.password_problems on the server)
  document.querySelectorAll("[data-password]").forEach(function (input) {
    var list = document.getElementById(input.getAttribute("data-password"));
    if (!list) return;
    var tests = {
      length: function (v) { return v.length > 8; },
      upper: function (v) { return /[A-Z]/.test(v); },
      lower: function (v) { return /[a-z]/.test(v); },
      special: function (v) { return /[^A-Za-z0-9]/.test(v); }
    };
    function check() {
      list.querySelectorAll("li").forEach(function (li) {
        li.classList.toggle("ok", tests[li.dataset.rule](input.value));
      });
    }
    input.addEventListener("input", check);
    check();
  });

  // Confirm before irreversible actions
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!window.confirm(el.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Variant pickers only show variants of the chosen product
  document.querySelectorAll("select[data-variants-for]").forEach(function (variantSelect) {
    var productSelect = document.getElementById(variantSelect.getAttribute("data-variants-for"));
    if (!productSelect) return;
    function filter() {
      var pid = productSelect.value;
      variantSelect.querySelectorAll("option[data-product]").forEach(function (opt) {
        var show = opt.dataset.product === pid;
        opt.hidden = !show;
        opt.disabled = !show;
        if (!show && opt.selected) variantSelect.value = "";
      });
    }
    productSelect.addEventListener("change", filter);
    filter();
  });

  // Choosing a quotation template fills in the plan for you
  var tpl = document.getElementById("template_id");
  var plan = document.getElementById("plan_id");
  if (tpl && plan) {
    function sync() {
      plan.disabled = !!tpl.value;
      plan.closest(".field").querySelector(".hint").textContent =
        tpl.value ? "Comes from the template." : "Pick a plan, or start from a template.";
    }
    tpl.addEventListener("change", sync);
    sync();
  }
})();
