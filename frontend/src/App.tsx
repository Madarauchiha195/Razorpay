import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, ArrowRight, BadgeCheck, BarChart3, Bot, BrainCircuit, Check,
  ChevronRight, CircleDollarSign, Clock3, Copy, CreditCard, Flame, Gauge,
  Gem, LockKeyhole, Menu, MessageSquareText, PackageCheck, Play, Plus,
  RefreshCcw, RotateCcw, Send, Settings2, ShieldAlert, ShieldCheck, ShieldX,
  Sparkles, TrendingUp, User, WalletCards, X,
} from "lucide-react";
import { Area, AreaChart, Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const API_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

type NavTab = "overview" | "negotiate" | "merchant" | "guard";
type StreamEvent = { type: string; message?: string; round?: number; code?: string; offer?: Offer; deal?: Deal; risk_score?: number; agent_frozen?: boolean; session_id?: string };
type Offer = { offer_id: string; offered_price: number; concessions: string[]; delivery_days: number; justification: string; negotiation_round: number; estimated_customer_value: number };
type Deal = { deal_id: string; product_id: string; product_name: string; final_price: number; concession_ids: string[]; policy_version: string; capability_id: string; authorization_id: string; expires_at: string; status: string; signature: string };
type Policy = {
  product_id: string; product_name: string; merchant_id: string; base_cost: number; target_price: number; min_acceptable_price: number; min_profit: number; max_discount: number; max_freebie_value: number; max_negotiation_rounds: number; max_daily_concession_budget: number; payment_fee_bps: number; payment_fixed_cost: number; flagship_product: boolean; human_approval_threshold: number; max_transactions: number; allowed_customer_segments: string[]; policy_version: string; authorization_expires_at: string;
  concessions: { id: string; name: string; merchant_cost: number; customer_perceived_value: number; inventory_available: boolean; allowed: boolean }[];
};
type Dashboard = { revenue: number; negotiations: number; deals_won: number; policy_blocks: number; profit_protected: number; average_concession_cost: number; average_customer_value: number; conversion: number; risk_events: number };
type GuardEvent = { id: number; session_id?: string; event_type: string; level: string; decision_code?: string; message: string; created_at: string };
type Product = { product_id: string; product_name: string; listing_price: number; flagship: boolean; policy_version: string };
type ActivityDay = { day: string; date: string; negotiations: number; authorized: number; blocked: number; revenue: number; concession: number; value: number };
type BlockReason = { code: string; name: string; count: number; percent: number };
type AgentState = { agent_frozen: boolean; violation_count: number; daily_concession_cost: number; transactions_today: number };
type ChatTurn = { role: "buyer" | "agent"; text: string; price?: number; code?: string; tone?: "approved" | "blocked"; at: string };
type ChatResponse = { session_id: string; turn: number; status: string; reply: string; code: string | null; offer: Offer | null; deal: Deal | null; price: number | null; engine: string | null; live_llm: boolean; message?: string };

const REASON_COLORS = ["#ff5d72", "#ffb45a", "#8c78ff", "#6879a6", "#4fd1a5", "#f472b6"];

const defaultPolicy: Policy = {
  product_id: "iphone-17-pro", product_name: "iPhone 17 Pro", merchant_id: "merchant_demo", base_cost: 135000, target_price: 150000,
  min_acceptable_price: 147000, min_profit: 10000, max_discount: 3000, max_freebie_value: 2000, max_negotiation_rounds: 3,
  max_daily_concession_budget: 30000, payment_fee_bps: 0, payment_fixed_cost: 0, flagship_product: true, human_approval_threshold: 147500,
  max_transactions: 100, allowed_customer_segments: ["retail", "loyal"], policy_version: "v1", authorization_expires_at: "",
  concessions: [
    { id: "warranty", name: "Extended Warranty", merchant_cost: 1200, customer_perceived_value: 4000, inventory_available: true, allowed: true },
    { id: "express", name: "Express Delivery", merchant_cost: 700, customer_perceived_value: 2500, inventory_available: true, allowed: true },
    { id: "case", name: "Premium Phone Case", merchant_cost: 600, customer_perceived_value: 2000, inventory_available: true, allowed: true },
    { id: "voucher", name: "Future Purchase Voucher", merchant_cost: 400, customer_perceived_value: 1500, inventory_available: true, allowed: true },
  ],
};

const emptyDashboard: Dashboard = { revenue: 0, negotiations: 0, deals_won: 0, policy_blocks: 0, profit_protected: 0, average_concession_cost: 0, average_customer_value: 0, conversion: 0, risk_events: 0 };
const currency = (value: number) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(value);
const compactCurrency = (value: number) => value >= 100000 ? `₹${(value / 100000).toFixed(1)}L` : currency(value);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`);
  if (!response.ok) throw new Error("Unable to reach DealMesh API");
  return response.json() as Promise<T>;
}

function NavItem({ active, icon: Icon, label, onClick }: { active: boolean; icon: typeof Activity; label: string; onClick: () => void }) {
  return <button onClick={onClick} className={`nav-item ${active ? "active" : ""}`}><Icon size={18} /><span>{label}</span>{active && <ChevronRight size={16} className="nav-chevron" />}</button>;
}

function StatusPill({ tone = "green", children }: { tone?: "green" | "blue" | "amber" | "red" | "slate"; children: React.ReactNode }) {
  return <span className={`status-pill ${tone}`}><span className="status-dot" />{children}</span>;
}

function MetricCard({ label, value, trend, icon: Icon, tone = "blue" }: { label: string; value: string; trend?: string; icon: typeof Activity; tone?: "blue" | "green" | "violet" | "amber" }) {
  return <article className="metric-card"><div className={`metric-icon ${tone}`}><Icon size={19} /></div><div><span>{label}</span><strong>{value}</strong>{trend && <small className={trend.startsWith("+") ? "up" : ""}>{trend}</small>}</div></article>;
}

function TimelineIcon({ event }: { event: StreamEvent }) {
  if (event.type.includes("BLOCK") || event.type.includes("FAILED")) return <ShieldX size={17} />;
  if (event.type.includes("AUTHORIZED")) return <BadgeCheck size={17} />;
  if (event.type.includes("OFFER")) return <Sparkles size={17} />;
  if (event.type.includes("START") || event.type.includes("ROUND")) return <Bot size={17} />;
  return <Activity size={17} />;
}

export default function App() {
  const [tab, setTab] = useState<NavTab>("overview");
  const [policy, setPolicy] = useState<Policy>(defaultPolicy);
  const [products, setProducts] = useState<Product[]>([]);
  const [productId, setProductId] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard>(emptyDashboard);
  const [activity, setActivity] = useState<ActivityDay[]>([]);
  const [reasons, setReasons] = useState<BlockReason[]>([]);
  const [events, setEvents] = useState<GuardEvent[]>([]);
  const [agentFrozen, setAgentFrozen] = useState(false);
  const [stream, setStream] = useState<StreamEvent[]>([]);
  const [budget, setBudget] = useState(145000);
  const [selectedBenefits, setSelectedBenefits] = useState(["warranty", "express"]);
  const [message, setMessage] = useState("I want the smartest package, not just the lowest price.");
  const [isNegotiating, setIsNegotiating] = useState(false);
  const [lastOffer, setLastOffer] = useState<Offer | null>(null);
  const [deal, setDeal] = useState<Deal | null>(null);
  const [paymentMessage, setPaymentMessage] = useState("");
  const [notice, setNotice] = useState("");
  const [mobileMenu, setMobileMenu] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [chat, setChat] = useState<ChatTurn[]>([]);
  const [chatBusy, setChatBusy] = useState(false);

  // Every figure below comes from the API. Nothing is padded with placeholder numbers.
  const refresh = async (id = productId) => {
    try {
      const query = id ? `?product_id=${encodeURIComponent(id)}` : "";
      const [savedPolicy, catalogue, savedDashboard, savedEvents, savedActivity, savedReasons, savedAgent] = await Promise.all([
        getJson<Policy>(`/api/merchant/policy${query}`), getJson<Product[]>("/api/products"),
        getJson<Dashboard>("/api/merchant/dashboard"), getJson<GuardEvent[]>("/api/dealguard/events"),
        getJson<ActivityDay[]>("/api/merchant/activity"), getJson<BlockReason[]>("/api/merchant/block-reasons"),
        getJson<AgentState>("/api/agent/state"),
      ]);
      setPolicy(savedPolicy); setProducts(catalogue); setProductId(savedPolicy.product_id);
      setDashboard(savedDashboard); setEvents(savedEvents); setActivity(savedActivity); setReasons(savedReasons);
      setAgentFrozen(savedAgent.agent_frozen);
      setNotice("");
    } catch {
      setNotice("Backend is offline — start FastAPI to enable live negotiations.");
    }
  };

  useEffect(() => { void refresh(); }, []);

  // Switching products re-reads that product's real policy, then re-bases the budget and
  // benefit selection so a ₹35k product is never negotiated with a ₹1.45L budget.
  const selectProduct = (id: string) => { setProductId(id); void refresh(id); };
  useEffect(() => {
    setBudget(Math.round((policy.target_price * 0.97) / 500) * 500);
    setSelectedBenefits((current) => {
      const available = policy.concessions.filter((item) => item.allowed && item.inventory_available).map((item) => item.id);
      const kept = current.filter((id) => available.includes(id));
      return kept.length > 0 ? kept : available.slice(0, 2);
    });
  }, [policy.product_id, policy.target_price]);

  const economics = useMemo(() => {
    const selected = policy.concessions.filter((item) => selectedBenefits.includes(item.id));
    const cost = selected.reduce((sum, item) => sum + item.merchant_cost, 0);
    const value = selected.reduce((sum, item) => sum + item.customer_perceived_value, 0);
    return { cost, value };
  }, [policy.concessions, selectedBenefits]);

  const pushChat = (turn: Omit<ChatTurn, "at">) =>
    setChat((existing) => [...existing, { ...turn, at: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }]);

  const addEvent = (event: StreamEvent) => {
    setStream((existing) => [...existing, event]);
    if (event.session_id) setSessionId(event.session_id);
    if (event.offer) setLastOffer(event.offer);
    if (event.deal) setDeal(event.deal);
    // The conversation view is a buyer-readable projection of the same guarded events, not a second
    // transcript: nothing appears here that DealGuard has not already ruled on.
    if (event.type === "OFFER_PROPOSED" && event.offer) pushChat({ role: "agent", text: event.offer.justification, price: event.offer.offered_price });
    else if (event.type === "DEAL_AUTHORIZED" && event.deal) pushChat({ role: "agent", text: "Signed and locked in. This authorization is time-limited and ready for payment.", price: event.deal.final_price, tone: "approved" });
    else if (event.type === "DEALGUARD_BLOCK") pushChat({ role: "agent", text: event.message ?? "DealGuard blocked that proposal.", code: event.code, tone: "blocked" });
    else if (event.type === "HUMAN_REVIEW_REQUIRED") pushChat({ role: "agent", text: event.message ?? "This offer needs merchant approval before payment.", tone: "blocked" });
    else if (event.type === "NEGOTIATION_FAILED") pushChat({ role: "agent", text: event.message ?? "The negotiation ended without an authorization.", code: event.code, tone: "blocked" });
  };

  const negotiate = async () => {
    setIsNegotiating(true); setStream([]); setLastOffer(null); setDeal(null); setPaymentMessage(""); setTab("negotiate");
    setSessionId(""); setChat([]);
    pushChat({ role: "buyer", text: message });
    try {
      const response = await fetch(`${API_URL}/api/negotiation/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: policy.product_id, product_name: policy.product_name, max_budget: budget, preferred_delivery_days: 2, priorities: ["value", "warranty", "fast delivery"], desired_freebies: selectedBenefits, customer_id: "buyer_demo", customer_segment: "retail", request_message: message }),
      });
      if (!response.ok || !response.body) throw new Error("Negotiation service unavailable");
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n"); buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          const raw = chunk.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
          if (raw) addEvent(JSON.parse(raw) as StreamEvent);
        }
      }
      await refresh();
    } catch {
      addEvent({ type: "NEGOTIATION_FAILED", message: "Could not connect to the backend. Check that the FastAPI server is running." });
    } finally { setIsNegotiating(false); }
  };

  // The message box is the buyer's live line to their agent: the first send opens a negotiation with
  // it, every later send is one more guarded round on the same session. The price the reply carries
  // has already been through DealGuard, so the chat can move the price without moving the boundary.
  const sendChat = async () => {
    const text = message.trim();
    if (!text || chatBusy || isNegotiating) return;
    if (!sessionId) { await negotiate(); return; }
    setChatBusy(true); pushChat({ role: "buyer", text }); setMessage("");
    try {
      const response = await fetch(`${API_URL}/api/negotiation/${sessionId}/message`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }),
      });
      const turn = await response.json() as ChatResponse;
      if (!response.ok) throw new Error(turn.message ?? "The negotiation service rejected this turn.");
      const settled = turn.status === "BLOCKED" || turn.status === "CLOSED" || turn.status === "LIMIT_REACHED" || turn.status === "FAILED";
      pushChat({ role: "agent", text: turn.reply, price: turn.price ?? undefined, code: turn.code ?? undefined, tone: turn.status === "AUTHORIZED" ? "approved" : settled ? "blocked" : undefined });
      if (turn.offer) setLastOffer(turn.offer);
      if (turn.deal) setDeal(turn.deal);
      // Also recorded in the decision stream, so the audit view stays the complete picture. Appended
      // directly rather than through addEvent, which would duplicate the chat bubble above.
      setStream((existing) => [...existing, { type: `CHAT_${turn.status}`, message: turn.reply, round: turn.turn, code: turn.code ?? undefined, offer: turn.offer ?? undefined, deal: turn.deal ?? undefined }]);
      await refresh();
    } catch {
      pushChat({ role: "agent", text: "Your agent could not reach the negotiation service. Check that the FastAPI server is running.", tone: "blocked" });
    } finally { setChatBusy(false); }
  };

  const setPolicyValue = (key: keyof Policy, value: number | boolean) => setPolicy((previous) => ({ ...previous, [key]: value } as Policy));  const savePolicy = async () => {
    try {
      const response = await fetch(`${API_URL}/api/merchant/policy`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(policy) });
      if (!response.ok) throw new Error("save failed");
      setPolicy(await response.json() as Policy); setNotice("Policy saved. New negotiations use the updated guardrails."); await refresh();
    } catch { setNotice("Policy could not be saved. Is the API running?"); }
  };

  const toggleFreeze = async (frozen: boolean) => {
    try { await fetch(`${API_URL}/api/agent/${frozen ? "freeze" : "reactivate"}`, { method: "POST" }); setNotice(frozen ? "Agent frozen. Autonomous proposals are disabled." : "Agent reactivated with a clean violation counter."); await refresh(); }
    catch { setNotice("The agent state could not be updated."); }
  };

  const payment = async () => {
    if (!deal) return;
    setPaymentMessage("Creating an order from the signed authorization…");
    try {
      const response = await fetch(`${API_URL}/api/payments/create`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ deal_id: deal.deal_id, authorization_id: deal.authorization_id, signature: deal.signature, idempotency_key: `ui_${crypto.randomUUID()}` }) });
      const order = await response.json() as { order_id?: string; live_checkout?: boolean; key_id?: string; amount?: number; currency?: string; message?: string };
      if (!response.ok || !order.order_id) throw new Error(order.message ?? "Order creation failed");
      if (!order.live_checkout || !order.key_id) { setPaymentMessage(`Mock order ${order.order_id} locked to this signed deal. Add Razorpay test keys to open Checkout.`); await refresh(); return; }
      await loadRazorpay();
      new window.Razorpay!({
        key: order.key_id, amount: order.amount, currency: order.currency, name: "DealMesh", description: `Authorized ${deal.product_name}`, order_id: order.order_id,
        theme: { color: "#1863D6" },
        handler: async (result: { razorpay_payment_id: string; razorpay_signature: string }) => {
          const verify = await fetch(`${API_URL}/api/payments/verify`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ deal_id: deal.deal_id, razorpay_order_id: order.order_id, razorpay_payment_id: result.razorpay_payment_id, razorpay_signature: result.razorpay_signature }) });
          setPaymentMessage(verify.ok ? "Payment verified server-side. Deal settled." : "Checkout returned, but server-side payment verification failed.");
          await refresh();
        },
      }).open();
      setPaymentMessage("Razorpay Test Mode checkout opened. Payment is not accepted until server verification succeeds.");
    } catch { setPaymentMessage("Unable to create a payment order. The deal remains protected."); }
  };

  const menu = <nav className={`side-nav ${mobileMenu ? "show" : ""}`}>
    <div className="brand"><div className="brand-mark"><span /><span /><span /></div><div><strong>DealMesh</strong><small>NEGOTIATION OS</small></div><button className="close-menu" onClick={() => setMobileMenu(false)}><X size={18} /></button></div>
    <div className="nav-label">WORKSPACE</div>
    <NavItem active={tab === "overview"} icon={Gauge} label="Command center" onClick={() => { setTab("overview"); setMobileMenu(false); }} />
    <NavItem active={tab === "negotiate"} icon={MessageSquareText} label="Buyer studio" onClick={() => { setTab("negotiate"); setMobileMenu(false); }} />
    <NavItem active={tab === "merchant"} icon={BarChart3} label="Merchant ops" onClick={() => { setTab("merchant"); setMobileMenu(false); }} />
    <NavItem active={tab === "guard"} icon={ShieldCheck} label="DealGuard" onClick={() => { setTab("guard"); setMobileMenu(false); }} />
    <div className="side-spacer" />
    <div className="protocol-card"><div className="protocol-icon"><LockKeyhole size={17} /></div><strong>Propose & Verify</strong><p>AI proposes. Code verifies. DealGuard authorizes.</p><StatusPill tone="green">Guardrails online</StatusPill></div>
    <div className="profile"><div className="avatar">DM</div><div><strong>Demo Merchant</strong><small>Control owner</small></div><Settings2 size={17} /></div>
  </nav>;

  return <div className="app-shell">
    {menu}
    <main className="main-content">
      <header className="topbar"><button className="mobile-menu" onClick={() => setMobileMenu(true)}><Menu size={21} /></button><div><p className="eyebrow">AGENTIC COMMERCE PROTOCOL</p><h1>{tab === "overview" ? "Command center" : tab === "negotiate" ? "Buyer negotiation studio" : tab === "merchant" ? "Merchant intelligence" : "DealGuard control room"}</h1></div><div className="topbar-actions"><StatusPill tone="green">Test environment</StatusPill><button className="icon-button" title="Refresh data" onClick={() => void refresh()}><RefreshCcw size={17} /></button><button className="primary-button compact" onClick={negotiate}><Play size={15} />Try negotiation</button></div></header>
      {notice && <div className="notice"><Activity size={16} />{notice}<button onClick={() => setNotice("")}><X size={15} /></button></div>}
      {tab === "overview" && <Overview dashboard={dashboard} policy={policy} activity={activity} onStart={negotiate} onTab={setTab} />}
      {tab === "negotiate" && <NegotiationStudio policy={policy} products={products} productId={productId} onProduct={selectProduct} budget={budget} setBudget={setBudget} selectedBenefits={selectedBenefits} setSelectedBenefits={setSelectedBenefits} message={message} setMessage={setMessage} economics={economics} stream={stream} offer={lastOffer} deal={deal} negotiating={isNegotiating} onStart={negotiate} onPayment={payment} paymentMessage={paymentMessage} chat={chat} chatBusy={chatBusy} sessionId={sessionId} onSend={sendChat} />}
      {tab === "merchant" && <MerchantOps dashboard={dashboard} policy={policy} events={events} activity={activity} reasons={reasons} />}
      {tab === "guard" && <GuardRoom policy={policy} events={events} frozen={agentFrozen} onNumber={setPolicyValue} onSave={savePolicy} onFreeze={toggleFreeze} />}
    </main>
  </div>;
}

function Overview({ dashboard, policy, activity, onStart, onTab }: { dashboard: Dashboard; policy: Policy; activity: ActivityDay[]; onStart: () => void; onTab: (tab: NavTab) => void }) {
  return <div className="page-stack overview-page">
    <section className="hero-card"><div className="hero-copy"><StatusPill tone="blue">AUTONOMOUS, BOUNDED, AUDITABLE</StatusPill><h2>Find the smartest deal.<br /><em>Never an unsafe one.</em></h2><p>DealMesh lets buyer and merchant agents negotiate customer value while a deterministic policy layer protects merchant economics.</p><div className="hero-actions"><button className="primary-button" onClick={onStart}>Start a negotiation <ArrowRight size={17} /></button><button className="ghost-button" onClick={() => onTab("guard")}>Inspect guardrails <ShieldCheck size={17} /></button></div></div><div className="mesh-visual"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="agent-node buyer"><Bot size={20} /><span>Buyer<br />Agent</span></div><div className="agent-node merchant"><BrainCircuit size={20} /><span>Merchant<br />Agent</span></div><div className="guard-core"><ShieldCheck size={25} /><strong>Deal<br />Guard</strong></div><div className="payment-node"><CreditCard size={16} /><span>Authorized payment</span></div><div className="signal-line line-a" /><div className="signal-line line-b" /></div></section>
    <section className="metric-grid"><MetricCard icon={WalletCards} tone="blue" label="Protected revenue" value={compactCurrency(dashboard.revenue)} trend={`${dashboard.deals_won} settled ${dashboard.deals_won === 1 ? "deal" : "deals"}`} /><MetricCard icon={MessageSquareText} tone="violet" label="Negotiations" value={String(dashboard.negotiations)} trend="all sessions recorded" /><MetricCard icon={BadgeCheck} tone="green" label="Deals won" value={String(dashboard.deals_won)} trend={`${dashboard.conversion}% conversion`} /><MetricCard icon={ShieldAlert} tone="amber" label="Policy blocks" value={String(dashboard.policy_blocks)} trend="All routes contained" /></section>
    <section className="two-column"><article className="panel chart-panel"><div className="panel-heading"><div><p className="eyebrow">NEGOTIATION CONVERSION</p><h3>Value protected over time</h3></div><button className="text-button" onClick={() => onTab("merchant")}>View analytics <ArrowRight size={15} /></button></div><div className="chart-wrap"><ResponsiveContainer width="100%" height={220}><AreaChart data={activity}><defs><linearGradient id="valueGradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#5c8dff" stopOpacity={0.45} /><stop offset="100%" stopColor="#5c8dff" stopOpacity={0} /></linearGradient></defs><XAxis dataKey="day" axisLine={false} tickLine={false} tick={{ fill: "#7180a8", fontSize: 12 }} /><YAxis hide /><Tooltip contentStyle={{ background: "#0e1538", border: "1px solid #26325d", borderRadius: 12 }} /><Area type="monotone" dataKey="value" stroke="#6f96ff" strokeWidth={3} fill="url(#valueGradient)" /></AreaChart></ResponsiveContainer></div></article><article className="panel protocol-flow"><div className="panel-heading"><div><p className="eyebrow">THE AUTHORIZATION PATH</p><h3>No payment bypass exists</h3></div><LockKeyhole size={19} className="muted-icon" /></div><div className="flow-list"><FlowRow icon={Bot} title="Buyer intent" text="Preferences and budget only" /><FlowRow icon={BrainCircuit} title="Merchant proposal" text="Structured, non-authoritative offer" /><FlowRow icon={ShieldCheck} title="DealGuard verification" text="Policy, profit, capability, risk" active /><FlowRow icon={CreditCard} title="Signed payment order" text="Razorpay Test Mode only" /></div></article></section>
    <section className="command-strip"><div><div className="pulse-icon"><Sparkles size={19} /></div><div><h3>Ready to negotiate {policy.product_name}?</h3><p>High-value concessions are ranked by customer value per rupee of merchant cost.</p></div></div><button className="primary-button" onClick={onStart}>Open buyer studio <ArrowRight size={17} /></button></section>
  </div>;
}

function FlowRow({ icon: Icon, title, text, active = false }: { icon: typeof Bot; title: string; text: string; active?: boolean }) { return <div className={`flow-row ${active ? "active" : ""}`}><div className="flow-icon"><Icon size={17} /></div><div><strong>{title}</strong><span>{text}</span></div>{active ? <StatusPill tone="green">Verified</StatusPill> : <ChevronRight size={16} />}</div>; }

function NegotiationStudio({ policy, products, productId, onProduct, budget, setBudget, selectedBenefits, setSelectedBenefits, message, setMessage, economics, stream, offer, deal, negotiating, onStart, onPayment, paymentMessage, chat, chatBusy, sessionId, onSend }: { policy: Policy; products: Product[]; productId: string; onProduct: (id: string) => void; budget: number; setBudget: (value: number) => void; selectedBenefits: string[]; setSelectedBenefits: (value: string[]) => void; message: string; setMessage: (value: string) => void; economics: { cost: number; value: number }; stream: StreamEvent[]; offer: Offer | null; deal: Deal | null; negotiating: boolean; onStart: () => void; onPayment: () => void; paymentMessage: string; chat: ChatTurn[]; chatBusy: boolean; sessionId: string; onSend: () => void }) {
  const toggleBenefit = (id: string) => setSelectedBenefits(selectedBenefits.includes(id) ? selectedBenefits.filter((item) => item !== id) : [...selectedBenefits, id]);
  // Budget bounds follow the selected product so the slider stays meaningful at every price point.
  const step = policy.target_price >= 100000 ? 500 : 100;
  const sliderMax = policy.target_price;
  const sliderMin = Math.floor((policy.target_price * 0.85) / step) * step;
  const maxPackageValue = policy.concessions.reduce((sum, item) => sum + item.customer_perceived_value, 0) || 1;
  return <div className="page-stack negotiation-page"><section className="studio-grid"><article className="panel buyer-input"><div className="panel-heading"><div><p className="eyebrow">BUYER AGENT</p><h3>Shape the ask</h3></div><div className="agent-presence buyer-presence"><Bot size={17} />Online</div></div><div className="product-mini"><div className="product-visual"><Gem size={27} /></div><div><span>NEGOTIATING FOR</span>{products.length > 0 ? <select className="product-select" value={productId} onChange={(event) => onProduct(event.target.value)} disabled={negotiating} aria-label="Choose a product to negotiate">{products.map((item) => <option key={item.product_id} value={item.product_id}>{item.product_name}</option>)}</select> : <strong>{policy.product_name}</strong>}<small>List price {currency(policy.target_price)}</small></div>{policy.flagship_product ? <StatusPill tone="blue">Flagship</StatusPill> : <StatusPill tone="slate">Standard</StatusPill>}</div><label className="field-label">Maximum budget <strong>{currency(budget)}</strong></label><input className="budget-range" type="range" min={sliderMin} max={sliderMax} step={step} value={budget} onChange={(event) => setBudget(Number(event.target.value))} /><div className="range-labels"><span>{compactCurrency(sliderMin)}</span><span>{compactCurrency(sliderMax)}</span></div><label className="field-label margin-top">Value preferences</label><div className="benefit-options">{policy.concessions.map((item) => <button key={item.id} className={`benefit-option ${selectedBenefits.includes(item.id) ? "selected" : ""}`} onClick={() => toggleBenefit(item.id)}><span className="check-circle">{selectedBenefits.includes(item.id) && <Check size={13} />}</span><span>{item.name}</span><small>+{currency(item.customer_perceived_value)} value</small></button>)}</div><label className="field-label margin-top">Message to your buyer agent</label><textarea value={message} onChange={(event) => setMessage(event.target.value)} maxLength={500} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); onSend(); } }} /><button className="ghost-button chat-send" onClick={onSend} disabled={negotiating || chatBusy || message.trim().length === 0}>{chatBusy ? <><RefreshCcw className="spin" size={15} />Agent is replying…</> : <><Send size={15} />{sessionId ? "Send to agent" : "Send & open negotiation"}</>}</button><button className="primary-button start-button" onClick={onStart} disabled={negotiating}>{negotiating ? <><RefreshCcw className="spin" size={17} />Negotiating safely…</> : <><Sparkles size={17} />Start agent negotiation</>}</button><p className="input-footnote"><LockKeyhole size={13} />The buyer agent never sees merchant floor, margin, or policy data.</p></article>
    <article className="panel live-feed"><div className="panel-heading"><div><p className="eyebrow">LIVE NEGOTIATION</p><h3>Agent decision stream</h3></div>{negotiating ? <StatusPill tone="blue">Streaming</StatusPill> : <StatusPill tone="slate">Awaiting intent</StatusPill>}</div><div className="feed-scroll">{stream.length === 0 && <div className="empty-feed"><div className="empty-orb"><MessageSquareText size={24} /></div><h4>A smarter deal starts here</h4><p>Submit your budget and priorities. Each proposal will be visible, validated, and auditable in real time.</p></div>}{stream.map((event, index) => <div className={`stream-row ${event.type.includes("BLOCK") || event.type.includes("FAILED") ? "blocked" : event.type.includes("AUTHORIZED") ? "approved" : ""}`} key={`${event.type}-${index}`}><div className="stream-icon"><TimelineIcon event={event} /></div><div className="stream-content"><div className="stream-meta"><strong>{humanType(event.type)}</strong>{event.round && <span>ROUND {event.round}</span>}</div><p>{event.message ?? event.code ?? "Event recorded"}</p>{event.offer && <OfferPreview offer={event.offer} />}{event.code && event.type.includes("BLOCK") && <span className="code-badge"><ShieldX size={12} />{event.code}</span>}</div></div>)}</div>{offer && !deal && <OfferPreview offer={offer} large />}</article>
    <aside className="studio-side"><article className="panel value-calculator"><div className="panel-heading"><div><p className="eyebrow">VALUE SIGNAL</p><h3>Package economics</h3></div><TrendingUp size={19} className="mint-icon" /></div><div className="value-number"><strong>{currency(offer?.estimated_customer_value ?? economics.value)}</strong><span>estimated customer value</span></div><div className="ratio-line"><span>Customer benefit mix</span><strong>{selectedBenefits.length || 0} items</strong></div><div className="ratio-bar"><span style={{ width: `${Math.min(100, ((offer?.estimated_customer_value ?? economics.value) / maxPackageValue) * 100)}%` }} /></div><p>The agent optimizes perceived customer value; DealGuard separately protects actual merchant costs.</p></article><article className="panel timeline-card"><div className="panel-heading"><div><p className="eyebrow">STATE MACHINE</p><h3>Bounded by design</h3></div></div><ol>{["Buyer intent", "Merchant analysis", "Structured offer", "DealGuard validation", "Signed deal"].map((step, index) => <li className={stream.length > index ? "done" : ""} key={step}><span>{stream.length > index ? <Check size={12} /> : index + 1}</span>{step}</li>)}</ol></article></aside></section>
    <ConversationPanel chat={chat} chatBusy={chatBusy} sessionId={sessionId} />
    {deal && <AuthorizedDealCard deal={deal} offer={offer} onPayment={onPayment} paymentMessage={paymentMessage} />}</div>;
}

function ConversationPanel({ chat, chatBusy, sessionId }: { chat: ChatTurn[]; chatBusy: boolean; sessionId: string }) {
  const tail = useRef<HTMLDivElement | null>(null);
  useEffect(() => { tail.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, [chat.length]);
  return <article className="panel chat-panel">
    <div className="panel-heading"><div><p className="eyebrow">LIVE CONVERSATION</p><h3>Buyer &harr; merchant agent</h3></div>{chatBusy ? <StatusPill tone="blue">Agent replying</StatusPill> : sessionId ? <StatusPill tone="green">Session open</StatusPill> : <StatusPill tone="slate">No session yet</StatusPill>}</div>
    <div className="chat-scroll">
      {chat.length === 0 && <div className="chat-empty"><MessageSquareText size={20} /><p>Write to your buyer agent on the left and press send. Each reply is a proposal DealGuard has already ruled on, so a price that moves here is a price the merchant has authorized.</p></div>}
      {chat.map((turn, index) => <div className={`chat-turn ${turn.role} ${turn.tone ?? ""}`} key={`${turn.role}-${index}`}>
        <div className="chat-avatar">{turn.role === "buyer" ? <User size={14} /> : <BrainCircuit size={14} />}</div>
        <div className="chat-bubble">
          <div className="chat-meta"><strong>{turn.role === "buyer" ? "You" : "Merchant agent"}</strong><span>{turn.at}</span></div>
          <p>{turn.text}</p>
          {(turn.price !== undefined || turn.code) && <div className="chat-tags">{turn.price !== undefined && <span className="chat-price"><Sparkles size={12} />{currency(turn.price)}</span>}{turn.code && <span className="code-badge"><ShieldX size={12} />{turn.code}</span>}</div>}
        </div>
      </div>)}
      <div ref={tail} />
    </div>
    <p className="input-footnote"><LockKeyhole size={13} />Your words reach the proposal model as preference text only. Every number is re-derived by DealGuard before it is shown, so a message cannot talk the price below the merchant floor.</p>
  </article>;
}

function OfferPreview({ offer, large = false }: { offer: Offer; large?: boolean }) { return <div className={`offer-preview ${large ? "large" : ""}`}><div><span>PROPOSED PACKAGE</span><strong>{currency(offer.offered_price)}</strong></div><div className="offer-benefits">{offer.concessions.map((item) => <span key={item}><Check size={12} />{item}</span>)}</div><p>{offer.justification}</p><div className="offer-footer"><span><Sparkles size={13} />{currency(offer.estimated_customer_value)} estimated value</span><span><Clock3 size={13} />{offer.delivery_days}-day delivery</span></div></div>; }

function AuthorizedDealCard({ deal, offer, onPayment, paymentMessage }: { deal: Deal; offer: Offer | null; onPayment: () => void; paymentMessage: string }) { const expires = new Date(deal.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); return <section className="authorized-card"><div className="auth-left"><div className="auth-check"><BadgeCheck size={25} /></div><div><p className="eyebrow">AUTHORIZED DEAL</p><h2>{currency(deal.final_price)} <span>for {deal.product_name}</span></h2><div className="auth-details"><span><PackageCheck size={14} />{offer?.concessions.join(" · ") || "Protected value package"}</span><span><Clock3 size={14} />Expires {expires}</span><span><ShieldCheck size={14} />Policy {deal.policy_version}</span></div></div></div><div className="auth-right"><StatusPill tone="green">Signed & verified</StatusPill><button className="primary-button" onClick={onPayment}><CreditCard size={17} />Proceed to Razorpay Test</button>{paymentMessage && <p className="payment-message">{paymentMessage}</p>}</div></section>; }

function MerchantOps({ dashboard, policy, events, activity, reasons }: { dashboard: Dashboard; policy: Policy; events: GuardEvent[]; activity: ActivityDay[]; reasons: BlockReason[] }) {
  const slices = reasons.map((item, index) => ({ name: item.name, value: item.percent, count: item.count, color: REASON_COLORS[index % REASON_COLORS.length] }));
  const efficiency = dashboard.average_concession_cost > 0 ? (dashboard.average_customer_value / dashboard.average_concession_cost).toFixed(1) : "—";
  return <div className="page-stack"><section className="merchant-head"><div><StatusPill tone="green">MERCHANT VIEW</StatusPill><h2>Growth without unbounded concessions.</h2><p>Every metric connects commercial performance to hard policy enforcement.</p></div><div className="merchant-head-stat"><span>PROFIT PROTECTED</span><strong>{compactCurrency(dashboard.profit_protected)}</strong><small><TrendingUp size={13} />value kept above the merchant floor</small></div></section><section className="metric-grid"><MetricCard icon={CircleDollarSign} tone="green" label="Revenue protected" value={compactCurrency(dashboard.revenue)} trend={`${dashboard.negotiations} negotiations`} /><MetricCard icon={PackageCheck} tone="blue" label="Deals closed" value={String(dashboard.deals_won)} trend={`${dashboard.conversion}% conversion`} /><MetricCard icon={Gem} tone="violet" label="Customer value" value={currency(dashboard.average_customer_value)} trend="per accepted deal" /><MetricCard icon={Flame} tone="amber" label="Risk events" value={String(dashboard.risk_events)} trend="All prevented" /></section><section className="analytics-grid"><article className="panel analytics-main"><div className="panel-heading"><div><p className="eyebrow">VALUE / COST EFFICIENCY</p><h3>Concessions create more value than they cost</h3></div><StatusPill tone="green">{efficiency}× return</StatusPill></div><ResponsiveContainer width="100%" height={260}><BarChart data={activity} barGap={8}><XAxis dataKey="day" axisLine={false} tickLine={false} tick={{ fill: "#7180a8", fontSize: 12 }} /><YAxis hide /><Tooltip cursor={{ fill: "#111a42" }} contentStyle={{ background: "#0e1538", border: "1px solid #26325d", borderRadius: 12 }} /><Bar dataKey="concession" radius={[5, 5, 0, 0]} fill="#566687" /><Bar dataKey="value" radius={[5, 5, 0, 0]} fill="#5f8dff" /></BarChart></ResponsiveContainer><div className="chart-legend"><span><i className="legend-slate" />Merchant concession cost</span><span><i className="legend-blue" />Customer perceived value</span></div></article><article className="panel reasons-card"><div className="panel-heading"><div><p className="eyebrow">GUARDRAIL OUTCOMES</p><h3>Why proposals were blocked</h3></div></div><div className="pie-wrap"><ResponsiveContainer width="100%" height={180}><PieChart><Pie data={slices} dataKey="value" innerRadius={54} outerRadius={78} paddingAngle={4}>{slices.map((item) => <Cell key={item.name} fill={item.color} />)}</Pie><Tooltip contentStyle={{ background: "#0e1538", border: "1px solid #26325d", borderRadius: 12 }} /></PieChart></ResponsiveContainer><div className="pie-total"><strong>{dashboard.policy_blocks}</strong><span>blocks</span></div></div><div className="reason-list">{slices.length === 0 && <div><span>No blocks recorded yet</span><strong>0%</strong></div>}{slices.map((item) => <div key={item.name}><span><i style={{ background: item.color }} />{item.name}</span><strong>{item.value}%</strong></div>)}</div></article></section><section className="panel concession-table"><div className="panel-heading"><div><p className="eyebrow">CONCESSION CATALOG</p><h3>Merchant-approved value levers</h3></div><StatusPill tone="green">Inventory synced</StatusPill></div><div className="table-wrap"><table><thead><tr><th>Concession</th><th>Merchant cost</th><th>Customer value</th><th>Value efficiency</th><th>Availability</th></tr></thead><tbody>{policy.concessions.map((item) => <tr key={item.id}><td><span className="concession-name"><span className="concession-gem"><Gem size={14} /></span>{item.name}</span></td><td>{currency(item.merchant_cost)}</td><td className="value-cell">{currency(item.customer_perceived_value)}</td><td><span className="efficiency">{(item.customer_perceived_value / item.merchant_cost).toFixed(1)}×</span></td><td><StatusPill tone={item.inventory_available ? "green" : "red"}>{item.inventory_available ? "Available" : "Out of stock"}</StatusPill></td></tr>)}</tbody></table></div></section>{events.length > 0 && <div className="recent-note"><Activity size={16} />{events.filter((item) => item.level === "APPROVED").length} recent authorization events are available in DealGuard.</div>}</div>;
}

function GuardRoom({ policy, events, frozen: serverFrozen, onNumber, onSave, onFreeze }: { policy: Policy; events: GuardEvent[]; frozen: boolean; onNumber: (key: keyof Policy, value: number | boolean) => void; onSave: () => void; onFreeze: (frozen: boolean) => void }) {
  // Freeze state is read from the server, not assumed, so a frozen agent is never shown as active.
  const [frozen, setFrozen] = useState(serverFrozen); const [copied, setCopied] = useState(false);
  useEffect(() => { setFrozen(serverFrozen); }, [serverFrozen]);
  const updateFreeze = (next: boolean) => { setFrozen(next); onFreeze(next); };
  const copyPolicy = async () => { await navigator.clipboard.writeText(policy.policy_version); setCopied(true); setTimeout(() => setCopied(false), 1500); };
  return <div className="page-stack guard-page"><section className="guard-hero"><div><StatusPill tone={frozen ? "red" : "green"}>{frozen ? "AGENT FROZEN" : "AUTONOMY ACTIVE"}</StatusPill><h2>The financial authorization boundary.</h2><p>Every number below is verified by code, not entrusted to an LLM or the frontend.</p></div><div className="guard-actions"><button className="ghost-button" onClick={() => updateFreeze(!frozen)}>{frozen ? <><RotateCcw size={16} />Reactivate agent</> : <><ShieldAlert size={16} />Freeze agent</>}</button><button className="primary-button" onClick={onSave}><Check size={16} />Publish policy</button></div></section><section className="guard-layout"><article className="panel policy-editor"><div className="panel-heading"><div><p className="eyebrow">POLICY CONTROLLER</p><h3>Delegated authority</h3></div><button className="copy-button" onClick={copyPolicy}><Copy size={14} />{copied ? "Copied" : policy.policy_version}</button></div><div className="policy-status"><div className="shield-emblem"><ShieldCheck size={26} /></div><div><strong>Policy is internally consistent</strong><span>Updated authorization expires according to merchant configuration.</span></div><StatusPill tone="green">SAFE</StatusPill></div><div className="policy-form"><PolicyInput label="Price floor" value={policy.min_acceptable_price} onChange={(value) => onNumber("min_acceptable_price", value)} helper="Hard block below this price" /><PolicyInput label="Minimum profit" value={policy.min_profit} onChange={(value) => onNumber("min_profit", value)} helper="Recalculated server-side" /><PolicyInput label="Maximum discount" value={policy.max_discount} onChange={(value) => onNumber("max_discount", value)} helper="Bounded agent authority" /><PolicyInput label="Freebie budget" value={policy.max_freebie_value} onChange={(value) => onNumber("max_freebie_value", value)} helper="Merchant cost, not retail value" /><PolicyInput label="Human review below" value={policy.human_approval_threshold} onChange={(value) => onNumber("human_approval_threshold", value)} helper="Yellow approval band" /><PolicyInput label="Daily concession cap" value={policy.max_daily_concession_budget} onChange={(value) => onNumber("max_daily_concession_budget", value)} helper="Circuit-breaker input" /></div><div className="switch-line"><div><strong>Flagship product protection</strong><span>Escalate out-of-bound offers on this product as critical</span></div><button className={`toggle ${policy.flagship_product ? "on" : ""}`} onClick={() => onNumber("flagship_product", !policy.flagship_product)} aria-label="Toggle flagship protection"><span /></button></div></article><article className="panel event-monitor"><div className="panel-heading"><div><p className="eyebrow">IMMUTABLE EVENT MONITOR</p><h3>DealGuard decisions</h3></div><StatusPill tone="blue">Live audit</StatusPill></div><div className="monitor-list">{events.length === 0 && <div className="empty-monitor"><ShieldCheck size={24} /><p>No decisions recorded yet. Run a negotiation to populate the tamper-evident audit trail.</p></div>}{events.map((event) => <div className={`monitor-item ${event.level.toLowerCase()}`} key={event.id}><div className="monitor-mark">{event.level === "BLOCKED" ? <ShieldX size={16} /> : event.level === "APPROVED" ? <ShieldCheck size={16} /> : <Activity size={16} />}</div><div><div><strong>{event.event_type.replaceAll("_", " ")}</strong><span>{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span></div><p>{event.message}</p>{event.decision_code && <code>{event.decision_code}</code>}</div></div>)}</div></article></section><section className="security-grid"><SecurityTile icon={LockKeyhole} title="Signed authorization" text="Canonical HMAC-SHA256 signature binds deal amount, buyer, policy, and expiry." /><SecurityTile icon={Clock3} title="Replay protection" text="A signed deal may create only one payment order before it expires." /><SecurityTile icon={ShieldX} title="No bypass route" text="Payment service rejects unsigned, modified, expired, or unapproved deals." /></section></div>;
}

function PolicyInput({ label, value, onChange, helper }: { label: string; value: number; onChange: (value: number) => void; helper: string }) { return <label className="policy-input"><span>{label}</span><div><i>₹</i><input type="number" min="0" value={value} onChange={(event) => onChange(Number(event.target.value))} /></div><small>{helper}</small></label>; }
function SecurityTile({ icon: Icon, title, text }: { icon: typeof LockKeyhole; title: string; text: string }) { return <article className="security-tile"><div><Icon size={19} /></div><h3>{title}</h3><p>{text}</p></article>; }
function humanType(type: string) { return type.replaceAll("_", " ").replace("DEALGUARD", "DealGuard").replace("OFFER", "Offer").replace("BUYER", "Buyer").replace("ROUND", "Round"); }

function loadRazorpay(): Promise<void> { if (window.Razorpay) return Promise.resolve(); return new Promise((resolve, reject) => { const script = document.createElement("script"); script.src = "https://checkout.razorpay.com/v1/checkout.js"; script.onload = () => resolve(); script.onerror = () => reject(new Error("Razorpay checkout failed to load")); document.body.appendChild(script); }); }
