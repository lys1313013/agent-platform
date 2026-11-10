import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Input, Modal, Select, Space, Tag } from 'antd';
import { Link } from 'react-router-dom';
import { localBrowserApi, type BrowserSelection, type BrowserState } from '@/lib/api/localBrowser';

interface Props { sessionId?: string | null; streaming?: boolean; available: boolean; onChange: (value: BrowserSelection | null) => void }
export default function LocalBrowserControl({ sessionId, streaming, available, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [deviceId, setDeviceId] = useState<string>();
  const [urls, setUrls] = useState('https://www.baidu.com\nhttps://www.jd.com');
  const [state, setState] = useState<BrowserState | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const { message, modal } = App.useApp();
  useEffect(() => {
    if (!available && !streaming) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      try {
        const result = sessionId ? await localBrowserApi.state(sessionId) : { enabled: available, devices: await localBrowserApi.devices(), sessions: [], pending: [] };
        if (live) {
          setState(result); setError('');
          timer = setTimeout(() => { void refresh(); }, open || streaming || result.sessions.length ? 2000 : 5000);
        }
      } catch (e) { if (live) { setState(null); setError(e instanceof Error ? e.message : '加载失败'); } }
    }
    void refresh();
    return () => { live = false; clearTimeout(timer); };
  }, [sessionId, available, streaming, open]);
  const trustedDevices = state?.devices.filter(d => d.persistent_authorized && d.online && !d.revoked) ?? [];
  const explicitDevice = state?.devices.find(d => d.id === deviceId);
  const trustedDevice = explicitDevice?.persistent_authorized && explicitDevice.online ? explicitDevice
    : !deviceId && trustedDevices.length === 1 ? trustedDevices[0] : undefined;
  const persistent = !!trustedDevice && !!state?.enabled && !paused && !error;
  const trustedDeviceId = trustedDevice?.id;
  useEffect(() => {
    onChange(persistent && trustedDeviceId ? { deviceId: trustedDeviceId, urls: [], persistent: true }
      : selected && deviceId && !error && state?.enabled ? { deviceId, urls: urls.split(/\s+/).filter(Boolean) } : null);
  }, [persistent, trustedDeviceId, selected, deviceId, urls, error, state?.enabled, onChange]);
  async function action(fn: () => Promise<unknown>) {
    setBusy(true);
    try { await fn(); if (sessionId) setState(await localBrowserApi.state(sessionId)); }
    catch (e) { message.error(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  }
  if (!available && !state?.sessions.length) return null;
  return <div className="mb-2 space-y-2">
    <Space wrap><Button size="small" onClick={() => setOpen(true)}>本地浏览器{persistent ? ' · 持续授权' : selected ? ' · 本轮启用' : ''}</Button>
      {persistent && <><Tag color="green">任意公开网站 · 普通操作免确认</Tag><Button size="small" disabled={streaming} onClick={() => { setPaused(true); setSelected(false); }}>本轮不使用</Button></>}
      {state?.sessions.map(s => <Space key={s.id}><Tag>{s.state === 'active' ? '浏览器运行中' : s.state}</Tag><Button size="small" danger loading={busy} onClick={() => action(() => localBrowserApi.stop(s.id))}>停止浏览器</Button>
        {s.state === 'interrupted' && <Button size="small" onClick={() => modal.confirm({ title: '确认本地任务已清理', content: '请先断开扩展，并在本地核对操作结果、关闭遗留任务窗口。确认后才会释放设备占用，不会重放操作。', onOk: () => action(() => localBrowserApi.cleanup(s.id)) })}>确认已清理</Button>}</Space>)}
    </Space>
    {state?.pending.map(c => <Card key={c.id} size="small" title="智能体请求操作你的本地浏览器">
      <p>{c.operation === 'navigate' ? '打开网站' : '页面操作'} · 有效期至 {new Date(c.expires_at).toLocaleTimeString()}</p>
      <div className="mb-3 space-y-1 break-all">
        {c.operation === 'navigate' ? <p>网址：{String(c.payload.url ?? '')}</p> : <>
          <p>动作：{{ click: '点击', fill: '填写', select: '选择', press: '按键', scroll: '滚动' }[String(c.payload.action)] ?? String(c.payload.action)}</p>
          {c.payload.ref != null && <p>页面目标：{String(c.payload.ref)}（请对照本地页面确认）</p>}
          {c.payload.value !== '' && c.payload.value != null && <p className="whitespace-pre-wrap">内容：{String(c.payload.value)}</p>}
          {c.payload.action === 'scroll' && <p>垂直滚动：{String(c.payload.delta_y)} 像素</p>}
        </>}
      </div>
      <Space><Button type="primary" loading={busy} onClick={() => action(() => localBrowserApi.approve(c.id, true, c.payload_digest))}>允许这次操作</Button><Button danger disabled={busy} onClick={() => action(() => localBrowserApi.approve(c.id, false, c.payload_digest))}>拒绝</Button></Space>
    </Card>)}
    <Modal title="本地浏览器授权" open={open} onCancel={() => setOpen(false)} footer={<Button onClick={() => setOpen(false)}>完成</Button>}>
      <div className="space-y-4">
        <p>仅控制本任务窗口。网页内容会发送给当前智能体的模型服务；聊天记录保留给你查看，不自动写入记忆、外部观测或其他工具。</p>
        <Link className="block" to="/local-browsers">管理 / 配对我的浏览器</Link>
        {error && <Alert type="error" title={error} />}
        {state && !state.enabled && <Alert type="info" title="请先在智能体的工具配置中启用本地浏览器工具。" />}
        <Select aria-label="选择本地浏览器" className="w-full" placeholder="选择在线浏览器" value={deviceId ?? trustedDevice?.id} disabled={streaming} onChange={id => { setDeviceId(id); setPaused(false); setSelected(false); }} options={state?.devices.map(d => ({ value: d.id, label: `${d.label} · ${d.online ? '在线' : '离线'}${d.persistent_authorized ? ' · 持续授权' : ''}`, disabled: !d.online || d.revoked }))} />
        {trustedDevice ? <>
          <Alert type="info" title="此设备已持续授权" description="允许任意公开网站，打开、点击和输入免确认。登录、验证码和支付由你接管。仅限当前用户及此会话所属租户，可在设备管理页撤销。" />
          <Checkbox checked={!paused} disabled={streaming} onChange={e => setPaused(!e.target.checked)}>本轮使用已授权浏览器</Checkbox>
        </> : <>
          <div><Checkbox checked={selected} disabled={streaming || !state?.enabled || !deviceId || !urls.trim()} onChange={e => setSelected(e.target.checked)}>发送下一条消息时授权（最长 30 分钟）</Checkbox></div>
          <label className="block">允许打开的网站（每行一个完整网址）<Input.TextArea value={urls} disabled={streaming} onChange={e => setUrls(e.target.value)} rows={3} /></label>
          <p>单轮授权需要逐次确认操作。开启设备的持续授权后，无需每轮选择网站。</p>
        </>}
      </div>
    </Modal>
  </div>;
}
