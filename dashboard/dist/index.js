(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input, Label } = SDK.components;
  const h = React.createElement;
  const API = "/api/plugins/learnhermes";

  const STATE_LABEL = { done: "done", sent: "sent", open: "open", dismissed: "dismissed" };
  const STATE_VARIANT = { done: "default", sent: "secondary", open: "outline", dismissed: "outline" };

  function JourneyRow({ d }) {
    return h("div", { className: "flex items-center gap-3 py-2 border-b border-border last:border-0" },
      h("span", { className: "text-xs text-muted-foreground w-5 text-right" }, d.n),
      h("div", { className: "flex-1 min-w-0" },
        h("div", { className: "text-sm font-medium" }, d.title),
        d.evidence ? h("div", { className: "text-xs text-muted-foreground truncate" }, d.evidence) : null,
      ),
      h(Badge, { variant: STATE_VARIANT[d.state] || "outline" }, STATE_LABEL[d.state] || d.state),
    );
  }

  function CurrentDispatch({ d, onDismiss, onSend, busy }) {
    if (!d) return null;
    return h(Card, { className: "border-ring" },
      h(CardHeader, null,
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, { className: "text-base" }, "Dispatch " + d.n + ": " + d.title),
          h(Badge, null, d.state === "sent" ? "awaiting you" : "up next"),
        ),
      ),
      h(CardContent, { className: "space-y-3" },
        h("p", { className: "text-sm text-muted-foreground" }, d.why),
        h("div", null,
          h(Label, { className: "text-xs" }, "Starter prompt — paste this to Hermes"),
          h("pre", { className: "text-xs bg-muted rounded-md p-3 mt-1 whitespace-pre-wrap font-mono" }, d.starter_prompt),
        ),
        h("div", { className: "flex items-center gap-2 flex-wrap" },
          h("a", { href: d.docs_url, target: "_blank", rel: "noreferrer", className: "text-xs underline text-muted-foreground" }, "Official guide"),
          h("div", { className: "flex-1" }),
          d.state !== "sent" ? h(Button, { size: "sm", variant: "outline", disabled: busy, onClick: onSend }, "Send ping now") : null,
          h(Button, { size: "sm", variant: "ghost", disabled: busy, onClick: onDismiss }, "Dismiss"),
        ),
        h("p", { className: "text-xs text-muted-foreground" },
          "Completion is verified from your real setup — no need to report back."),
      ),
    );
  }

  function Settings({ settings, targets, onSave, busy }) {
    const [form, setForm] = useState(settings);
    useEffect(() => setForm(settings), [settings]);
    const set = (k) => (e) => {
      const v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      setForm({ ...form, [k]: v });
    };
    const submit = () => onSave({
      enabled: !!form.enabled,
      channel: String(form.channel || ""),
      dashboard_url: String(form.dashboard_url || ""),
      send_start: parseInt(form.send_start, 10) || 0,
      send_end: parseInt(form.send_end, 10) || 0,
      max_per_day: parseInt(form.max_per_day, 10) || 1,
    });
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Dispatch settings")),
      h(CardContent, { className: "space-y-3" },
        h("div", { className: "flex items-center gap-2" },
          h("input", { type: "checkbox", checked: !!form.enabled, onChange: set("enabled"), id: "lh-enabled" }),
          h(Label, { htmlFor: "lh-enabled", className: "text-sm" }, "Send dispatches"),
        ),
        h("div", null,
          h(Label, { className: "text-xs" }, "Channel (any hermes send target: telegram, discord:#channel, push, email...)"),
          h(Input, { value: form.channel || "", onChange: set("channel"), placeholder: "telegram", className: "mt-1" }),
          h("p", { className: "text-xs text-muted-foreground mt-1" },
            "Detected platforms: " + ((targets && targets.length && targets.join(", ")) || "none")),
        ),
        h("div", null,
          h(Label, { className: "text-xs" }, "Dashboard link sent in pings (use a reachable address for phone)"),
          h(Input, { value: form.dashboard_url || "", onChange: set("dashboard_url"), className: "mt-1" }),
        ),
        h("div", { className: "grid grid-cols-3 gap-2" },
          h("div", null, h(Label, { className: "text-xs" }, "Send from (hour)"), h(Input, { type: "number", min: 0, max: 23, value: form.send_start, onChange: set("send_start"), className: "mt-1" })),
          h("div", null, h(Label, { className: "text-xs" }, "Send until (hour)"), h(Input, { type: "number", min: 0, max: 23, value: form.send_end, onChange: set("send_end"), className: "mt-1" })),
          h("div", null, h(Label, { className: "text-xs" }, "Max / day"), h(Input, { type: "number", min: 1, max: 10, value: form.max_per_day, onChange: set("max_per_day"), className: "mt-1" })),
        ),
        h(Button, { size: "sm", disabled: busy, onClick: submit }, "Save settings"),
      ),
    );
  }

  function LearnHermesPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const load = useCallback(() => {
      SDK.fetchJSON(API + "/journey").then(setData).catch((e) => setError(String(e)));
    }, []);
    useEffect(load, [load]);

    const act = (path, body) => {
      setBusy(true);
      setError("");
      SDK.fetchJSON(API + path, { method: "POST", body: body ? JSON.stringify(body) : undefined })
        .then((r) => { if (r && r.ok === false) setError(r.error || "action failed"); load(); })
        .catch((e) => setError(String(e)))
        .finally(() => setBusy(false));
    };

    if (error && !data) return h(Card, null, h(CardContent, { className: "py-4 text-sm" }, "LearnHermes failed to load: " + error));
    if (!data) return h(Card, null, h(CardContent, { className: "py-4 text-sm text-muted-foreground" }, "Loading your journey..."));

    return h("div", { className: "space-y-4 max-w-3xl" },
      h("div", { className: "flex items-center gap-2 flex-wrap" },
        h("h1", { className: "text-lg font-semibold" }, "LearnHermes"),
        h(Badge, { variant: "secondary" }, "pace: " + data.pace),
        h(Badge, { variant: "outline" }, data.milestones_done + " / " + data.milestones_total + " milestones"),
        h(Badge, { variant: "outline" }, data.sessions_7d + " sessions this week"),
        data.settings.enabled ? null : h(Badge, { variant: "outline" }, "paused"),
        data.errors_48h > 0 ? h(Badge, { variant: "outline" }, data.errors_48h + " errors (48h)") : null,
        h("a", { href: API + "/snapshot", target: "_blank", rel: "noreferrer", className: "text-xs underline text-muted-foreground ml-auto" }, "Mobile snapshot"),
      ),
      error ? h("div", { className: "text-xs text-destructive" }, error) : null,
      h(CurrentDispatch, {
        d: data.current,
        busy: busy,
        onDismiss: () => data.current && act("/dispatch/" + data.current.id + "/dismiss"),
        onSend: () => act("/send-now"),
      }),
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "The journey")),
        h(CardContent, null, data.dispatches.map((d) => h(JourneyRow, { key: d.id, d: d }))),
      ),
      h(Settings, { settings: data.settings, targets: data.gateway_targets, busy: busy, onSave: (s) => act("/settings", s) }),
      h("p", { className: "text-xs text-muted-foreground" },
        "Milestones are verified from real state: cron jobs, skills, profiles, sessions. " +
        "Dispatches wait for natural pauses — never mid-task."),
    );
  }

  window.__HERMES_PLUGINS__.register("learnhermes", LearnHermesPage);
})();
