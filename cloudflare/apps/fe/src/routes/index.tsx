import { type FormEvent, useEffect, useMemo, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { trpc } from "@/utils/trpc";
import { REST_API_BASE } from "@/utils/urls";

export const Route = createFileRoute("/")({
    component: Dashboard,
});

type AuthPayload = {
  user: {
    id: string;
    name: string;
    email: string;
    role: string;
  };
  organization: {
    id: number;
    name: string;
    domain: string | null;
    industry: string | null;
    targetCustomer: string | null;
    product: string | null;
    conversionGoal: string | null;
    onboardingCompleted: number;
  };
  trial: {
    trialSecondsAllocated: number;
    trialSecondsUsed: number;
    trialSecondsRemaining: number;
  };
};

function Dashboard() {
  const auth = trpc.auth.me.useQuery();
  const me = (auth.data as AuthPayload | null) ?? null;
  const isAuthed = Boolean(me?.user);

  const trial = trpc.usage.trial.useQuery(undefined, {
    enabled: isAuthed,
  });
  const campaignList = trpc.campaigns.list.useQuery(undefined, {
    enabled: isAuthed,
  });
  const leadList = trpc.leads.list.useQuery(undefined, {
    enabled: isAuthed,
  });
  const callList = trpc.calls.list.useQuery({ limit: 10, offset: 0 }, {
    enabled: isAuthed,
  });
  const templateList = trpc.templates.list.useQuery(undefined, {
    enabled: isAuthed,
  });
  const organizationQuery = trpc.organization.get.useQuery(undefined, {
    enabled: isAuthed,
  });

  const createCampaign = trpc.campaigns.create.useMutation({
    onSuccess: () => {
      campaignList.refetch();
      setCampaignForm((prev) => ({ ...prev, name: "", prompt: "", status: "active", templateId: "" }));
    },
  });
  const createLead = trpc.leads.create.useMutation({
    onSuccess: () => {
      leadList.refetch();
      setLeadForm({ name: "", phone: "", email: "", campaignId: "", company: "", status: "", problem: "", budget: "", timeline: "", teamSize: "", currentTools: "", interactionHistory: "", notes: "" });
    },
  });
  const createCall = trpc.calls.create.useMutation({
    onSuccess: () => {
      callList.refetch();
      setCallForm({ callSid: "", fromNumber: "", toNumber: "", campaignId: "", leadId: "" });
    },
  });
  const onboardingMutation = trpc.organization.upsertOnboarding.useMutation({
    onSuccess: () => {
      organizationQuery.refetch();
      auth.refetch();
    },
  });

  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [loginForm, setLoginForm] = useState({ email: "", password: "" });
  const [registerForm, setRegisterForm] = useState({
    ownerName: "",
    email: "",
    password: "",
    companyName: "",
    domain: "",
    industry: "",
    targetCustomer: "",
    product: "",
    conversionGoal: "",
  });
  const [onboardingForm, setOnboardingForm] = useState({
    name: "",
    domain: "",
    industry: "",
    targetCustomer: "",
    product: "",
    conversionGoal: "",
  });

  const [authMessage, setAuthMessage] = useState("");

  const [campaignForm, setCampaignForm] = useState({
    name: "",
    prompt: "",
    systemPrompt: "",
    campaignContext: "",
    leadContextTemplate: "",
    templateId: "",
    status: "active",
  });
  const [leadForm, setLeadForm] = useState({
    name: "",
    phone: "",
    email: "",
    campaignId: "",
    company: "",
    status: "",
    problem: "",
    budget: "",
    timeline: "",
    teamSize: "",
    currentTools: "",
    interactionHistory: "",
    notes: "",
  });
  const [callForm, setCallForm] = useState({
    callSid: "",
    fromNumber: "",
    toNumber: "",
    campaignId: "",
    leadId: "",
  });

  const authHeaders = {
    "content-type": "application/json",
  };

  const submitAuth = async (path: "/auth/register" | "/auth/login", payload: Record<string, string>) => {
    const response = await fetch(`${REST_API_BASE}${path}`, {
      method: "POST",
      headers: authHeaders,
      credentials: "include",
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result?.error ?? "Authentication failed");
    }
    return result;
  };

  const submitOnboarding = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!organizationQuery.data) return;
    if (!onboardingForm.name || !onboardingForm.domain) {
      return;
    }
    await onboardingMutation.mutateAsync({
      name: onboardingForm.name,
      domain: onboardingForm.domain,
      industry: onboardingForm.industry || undefined,
      targetCustomer: onboardingForm.targetCustomer || undefined,
      product: onboardingForm.product || undefined,
      conversionGoal: onboardingForm.conversionGoal || undefined,
    });
  };

  const logout = async () => {
    await fetch(`${REST_API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
    auth.refetch();
  };

  useEffect(() => {
    if (organizationQuery.data) {
      setOnboardingForm({
        name: organizationQuery.data.name,
        domain: organizationQuery.data.domain ?? "",
        industry: organizationQuery.data.industry ?? "",
        targetCustomer: organizationQuery.data.targetCustomer ?? "",
        product: organizationQuery.data.product ?? "",
        conversionGoal: organizationQuery.data.conversionGoal ?? "",
      });
    }
  }, [organizationQuery.data]);

  const activeCalls = useMemo(
    () => callList.data?.filter((row: any) => row.status === "active") ?? [],
    [callList.data],
  );

  const defaultTemplate = templateList.data?.[0];
  const trialSecondsRemaining = trial.data?.trialSecondsRemaining ?? me?.trial?.trialSecondsRemaining ?? 0;
  const trialMinutesRemaining = Math.max(0, Math.round((trialSecondsRemaining / 60) * 10) / 10);

  if (!isAuthed) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="mx-auto max-w-4xl px-4 py-8">
          <h1 className="text-3xl font-black tracking-wide text-sky-100">Rapid-X Voice SaaS</h1>
          <p className="mt-2 text-sm text-slate-300">
            Sign in or create a workspace to manage campaigns, leads, and live-call usage.
          </p>

          <div className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
            <div className="flex gap-2">
              <button
                className="rounded-md bg-sky-500 px-4 py-2 font-semibold text-slate-950"
                onClick={() => setAuthMode("login")}
              >
                Login
              </button>
              <button
                className="rounded-md border border-slate-700 px-4 py-2"
                onClick={() => setAuthMode("register")}
              >
                Register
              </button>
            </div>

            {authMessage && <p className="mt-4 rounded-md bg-rose-900/50 px-3 py-2 text-sm text-rose-100">{authMessage}</p>}

            {authMode === "login" && (
              <form
                className="mt-6 space-y-3"
                onSubmit={async (event) => {
                  event.preventDefault();
                  setAuthMessage("");
                  try {
                    await submitAuth("/auth/login", loginForm);
                    setLoginForm({ email: "", password: "" });
                    await auth.refetch();
                  } catch (error: any) {
                    setAuthMessage(error?.message ?? "Login failed");
                  }
                }}
              >
                <h2 className="text-lg font-semibold">Login</h2>
                <input
                  value={loginForm.email}
                  onChange={(event) => setLoginForm({ ...loginForm, email: event.target.value })}
                  placeholder="Email"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={loginForm.password}
                  onChange={(event) => setLoginForm({ ...loginForm, password: event.target.value })}
                  placeholder="Password"
                  type="password"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <button
                  type="submit"
                  disabled={auth.isFetching}
                  className="rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950"
                >
                  {auth.isFetching ? "Logging in..." : "Login"}
                </button>
              </form>
            )}

            {authMode === "register" && (
              <form
                className="mt-6 space-y-3"
                onSubmit={async (event) => {
                  event.preventDefault();
                  setAuthMessage("");
                  if (registerForm.password.length < 8) {
                    setAuthMessage("Password should be at least 8 characters.");
                    return;
                  }
                  try {
                    await submitAuth("/auth/register", registerForm);
                    setRegisterForm({
                      ownerName: "",
                      email: "",
                      password: "",
                      companyName: "",
                      domain: "",
                      industry: "",
                      targetCustomer: "",
                      product: "",
                      conversionGoal: "",
                    });
                    await auth.refetch();
                  } catch (error: any) {
                    setAuthMessage(error?.message ?? "Register failed");
                  }
                }}
              >
                <h2 className="text-lg font-semibold">Create workspace</h2>
                <input
                  value={registerForm.ownerName}
                  onChange={(event) => setRegisterForm({ ...registerForm, ownerName: event.target.value })}
                  placeholder="Owner name"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.email}
                  onChange={(event) => setRegisterForm({ ...registerForm, email: event.target.value })}
                  placeholder="Work email"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.password}
                  onChange={(event) => setRegisterForm({ ...registerForm, password: event.target.value })}
                  placeholder="Password"
                  type="password"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.companyName}
                  onChange={(event) => setRegisterForm({ ...registerForm, companyName: event.target.value })}
                  placeholder="Company name"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.domain}
                  onChange={(event) => setRegisterForm({ ...registerForm, domain: event.target.value })}
                  placeholder="Company domain (example.com)"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.industry}
                  onChange={(event) => setRegisterForm({ ...registerForm, industry: event.target.value })}
                  placeholder="Industry (optional)"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.targetCustomer}
                  onChange={(event) => setRegisterForm({ ...registerForm, targetCustomer: event.target.value })}
                  placeholder="Target customer"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.product}
                  onChange={(event) => setRegisterForm({ ...registerForm, product: event.target.value })}
                  placeholder="Product/offer"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <input
                  value={registerForm.conversionGoal}
                  onChange={(event) => setRegisterForm({ ...registerForm, conversionGoal: event.target.value })}
                  placeholder="Main conversion goal"
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
                />
                <button
                  type="submit"
                  disabled={auth.isFetching}
                  className="rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950"
                >
                  {auth.isFetching ? "Creating account..." : "Create account"}
                </button>
              </form>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (me && me.organization?.onboardingCompleted !== 1) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="mx-auto max-w-4xl px-4 py-8">
          <h1 className="text-3xl font-black tracking-wide text-sky-100">Rapid-X Voice SaaS</h1>
          <p className="mt-2 text-sm text-slate-300">Finish onboarding to start outbound AI calling.</p>

          <form onSubmit={submitOnboarding} className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
            <h2 className="text-lg font-semibold">Onboarding</h2>
            <input
              value={onboardingForm.name}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, name: event.target.value })}
              placeholder="Company name"
              className="mt-4 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <input
              value={onboardingForm.domain}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, domain: event.target.value })}
              placeholder="Domain"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <input
              value={onboardingForm.industry}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, industry: event.target.value })}
              placeholder="Industry"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <input
              value={onboardingForm.targetCustomer}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, targetCustomer: event.target.value })}
              placeholder="Target customer"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <input
              value={onboardingForm.product}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, product: event.target.value })}
              placeholder="Product/offer"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <input
              value={onboardingForm.conversionGoal}
              onChange={(event) => setOnboardingForm({ ...onboardingForm, conversionGoal: event.target.value })}
              placeholder="Conversion goal"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <button
              type="submit"
              disabled={onboardingMutation.isPending}
              className="mt-4 rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950"
            >
              {onboardingMutation.isPending ? "Saving..." : "Save onboarding"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-6xl px-4 py-8">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-black tracking-wide text-sky-100">Rapid-X Voice Ops</h1>
            <p className="mt-2 text-sm text-slate-300">
              Organization: {me?.organization?.name} / User: {me?.user?.name}
            </p>
          </div>
          <button onClick={logout} className="rounded-lg border border-slate-700 px-3 py-2">
            Logout
          </button>
        </div>

        <section className="mt-8 grid gap-4 md:grid-cols-4">
          <StatCard label="Campaigns" value={campaignList.data?.length ?? 0} />
          <StatCard label="Leads" value={leadList.data?.length ?? 0} />
          <StatCard label="Active calls" value={activeCalls.length} />
          <StatCard
            label="Trial minutes"
            value={trialMinutesRemaining}
          />
        </section>

        <section className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
          <h2 className="text-lg font-semibold">Choose Template Library</h2>
          <ul className="mt-3 space-y-2 text-sm text-slate-300">
            {templateList.data?.map((template: any) => (
              <li key={template.id} className="rounded-lg border border-slate-700 px-3 py-2">
                <div className="font-semibold">{template.name}</div>
                <div className="text-xs opacity-70">
                  {template.systemPromptTemplate.slice(0, 110)}...
                </div>
              </li>
            ))}
          </ul>
        </section>

        <section className="mt-8 grid gap-4 xl:grid-cols-3">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!campaignForm.name || !campaignForm.prompt) return;
              createCampaign.mutate({
                name: campaignForm.name,
                templateId: campaignForm.templateId ? Number(campaignForm.templateId) : undefined,
                prompt: campaignForm.prompt || defaultTemplate?.campaignContextTemplate,
                systemPrompt: campaignForm.systemPrompt || defaultTemplate?.systemPromptTemplate || undefined,
                campaignContext: campaignForm.campaignContext || defaultTemplate?.campaignContextTemplate || undefined,
                leadContextTemplate: campaignForm.leadContextTemplate || defaultTemplate?.leadContextTemplate || undefined,
                status: campaignForm.status as "active" | "paused",
              });
            }}
            className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5"
          >
            <h2 className="text-lg font-semibold">Create Campaign</h2>
            <label className="mt-4 block text-xs text-slate-300">Campaign name</label>
            <input
              value={campaignForm.name}
              onChange={(event) => setCampaignForm({ ...campaignForm, name: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Template</label>
            <select
              value={campaignForm.templateId}
              onChange={(event) => {
                const template = templateList.data?.find((row: any) => String(row.id) === event.target.value);
                setCampaignForm({
                  ...campaignForm,
                  templateId: event.target.value,
                  prompt: template?.campaignContextTemplate ?? campaignForm.prompt,
                  systemPrompt: template?.systemPromptTemplate ?? campaignForm.systemPrompt,
                  campaignContext: template?.campaignContextTemplate ?? campaignForm.campaignContext,
                  leadContextTemplate: template?.leadContextTemplate ?? campaignForm.leadContextTemplate,
                });
              }}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            >
              <option value="">Select template</option>
              {templateList.data?.map((template: any) => (
                <option key={template.id} value={template.id}>
                  {template.name}
                </option>
              ))}
            </select>
            <label className="mt-3 block text-xs text-slate-300">Prompt</label>
            <textarea
              value={campaignForm.prompt}
              onChange={(event) => setCampaignForm({ ...campaignForm, prompt: event.target.value })}
              className="mt-1 h-20 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
              placeholder="Campaign objective/prompt"
            />
            <button
              type="submit"
              className="mt-4 rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950 hover:bg-sky-400"
              disabled={createCampaign.isPending}
            >
              {createCampaign.isPending ? "Saving..." : "Save Campaign"}
            </button>
          </form>

          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!leadForm.name || !leadForm.phone) return;
              createLead.mutate({
                name: leadForm.name,
                phone: leadForm.phone,
                email: leadForm.email || undefined,
                campaignId: leadForm.campaignId ? Number(leadForm.campaignId) : undefined,
                company: leadForm.company || undefined,
                status: leadForm.status || undefined,
                problem: leadForm.problem || undefined,
                budget: leadForm.budget || undefined,
                timeline: leadForm.timeline || undefined,
                teamSize: leadForm.teamSize || undefined,
                currentTools: leadForm.currentTools || undefined,
                interactionHistory: leadForm.interactionHistory || undefined,
                notes: leadForm.notes || undefined,
              });
            }}
            className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5"
          >
            <h2 className="text-lg font-semibold">Create Lead</h2>
            <label className="mt-4 block text-xs text-slate-300">Lead name</label>
            <input
              value={leadForm.name}
              onChange={(event) => setLeadForm({ ...leadForm, name: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Phone</label>
            <input
              value={leadForm.phone}
              onChange={(event) => setLeadForm({ ...leadForm, phone: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Email</label>
            <input
              value={leadForm.email}
              onChange={(event) => setLeadForm({ ...leadForm, email: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Company</label>
            <input
              value={leadForm.company}
              onChange={(event) => setLeadForm({ ...leadForm, company: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Campaign id</label>
            <input
              value={leadForm.campaignId}
              onChange={(event) => setLeadForm({ ...leadForm, campaignId: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
              placeholder="e.g. 1"
            />
            <button
              type="submit"
              className="mt-4 rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950 hover:bg-sky-400"
              disabled={createLead.isPending}
            >
              {createLead.isPending ? "Saving..." : "Save Lead"}
            </button>
          </form>

          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!callForm.callSid || !callForm.fromNumber || !callForm.toNumber) return;
              createCall.mutate({
                callSid: callForm.callSid,
                fromNumber: callForm.fromNumber,
                toNumber: callForm.toNumber,
                campaignId: callForm.campaignId ? Number(callForm.campaignId) : undefined,
                leadId: callForm.leadId ? Number(callForm.leadId) : undefined,
                status: "queued",
              });
            }}
            className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5"
          >
            <h2 className="text-lg font-semibold">Queue Call Record</h2>
            <label className="mt-4 block text-xs text-slate-300">Call SID</label>
            <input
              value={callForm.callSid}
              onChange={(event) => setCallForm({ ...callForm, callSid: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
              placeholder="call-xxxxxxxx"
            />
            <label className="mt-3 block text-xs text-slate-300">From</label>
            <input
              value={callForm.fromNumber}
              onChange={(event) => setCallForm({ ...callForm, fromNumber: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">To</label>
            <input
              value={callForm.toNumber}
              onChange={(event) => setCallForm({ ...callForm, toNumber: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Campaign id (optional)</label>
            <input
              value={callForm.campaignId}
              onChange={(event) => setCallForm({ ...callForm, campaignId: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <label className="mt-3 block text-xs text-slate-300">Lead id (optional)</label>
            <input
              value={callForm.leadId}
              onChange={(event) => setCallForm({ ...callForm, leadId: event.target.value })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2"
            />
            <button
              type="submit"
              className="mt-4 rounded-lg bg-sky-500 px-4 py-2 font-semibold text-slate-950 hover:bg-sky-400"
              disabled={createCall.isPending}
            >
              {createCall.isPending ? "Saving..." : "Queue Record"}
            </button>
          </form>
        </section>

        <section className="mt-10 rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
          <h2 className="text-lg font-semibold">Recent Calls</h2>
          <div className="mt-4 overflow-auto">
            <table className="w-full table-auto text-sm">
              <thead className="text-left text-slate-300">
                <tr>
                  <th className="px-2 py-2">Call SID</th>
                  <th className="px-2 py-2">From</th>
                  <th className="px-2 py-2">To</th>
                  <th className="px-2 py-2">Status</th>
                  <th className="px-2 py-2">Duration (s)</th>
                  <th className="px-2 py-2">Updated</th>
                </tr>
              </thead>
              <tbody>
                {callList.data?.map((call: any) => (
                  <tr key={call.callSid} className="border-t border-slate-800">
                    <td className="px-2 py-2 font-mono text-xs">{call.callSid}</td>
                    <td className="px-2 py-2">{call.fromNumber}</td>
                    <td className="px-2 py-2">{call.toNumber}</td>
                    <td className="px-2 py-2 capitalize">{call.status}</td>
                    <td className="px-2 py-2">{call.durationSeconds ?? 0}</td>
                    <td className="px-2 py-2">{new Date(call.updatedAt * 1000).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {callList.isLoading && <p className="mt-4 text-sm text-slate-400">Loading calls...</p>}
          {callList.error && <p className="mt-4 text-sm text-rose-400">{String(callList.error)}</p>}
        </section>
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
      <div className="text-sm text-slate-300">{label}</div>
      <div className="mt-2 text-3xl font-black tracking-wide">{value}</div>
    </div>
  );
}
