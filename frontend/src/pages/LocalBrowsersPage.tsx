import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Input, Popconfirm, Space, Tag, Typography, Skeleton } from 'antd';
import { localBrowserApi, type BrowserDevice } from '@/lib/api/localBrowser';
import { useAuthStore } from '@/stores/authStore';

export default function LocalBrowsersPage() {
  const tenant = useAuthStore(s => s.tenantId);
  return <Devices key={tenant} />;
}
function Devices() {
  const [devices, setDevices] = useState<BrowserDevice[]>([]);
  const [pairing, setPairing] = useState<{ url: string; expires_at: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const requestId = useRef(crypto.randomUUID());
  const { message } = App.useApp();
  const refresh = useCallback(async () => {
    try { setDevices(await localBrowserApi.devices()); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : '加载失败'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); const timer = setInterval(() => { void refresh(); }, 5000); return () => clearInterval(timer); }, [refresh]);
  return <div className="min-h-0 flex-1 overflow-y-auto"><div className="page-content mx-auto max-w-3xl space-y-4">
    <Typography.Title level={3}>我的本地浏览器</Typography.Title>
    <p>连接你的 Chrome 后，智能体可以操作你的本地浏览器。可选择逐轮授权，或开启持续授权，随时停止或撤销。</p>
    <Alert type="info" showIcon title="先安装 BrowserSkill 官方 Chrome 扩展" description={<span>在扩展的远程连接设置中粘贴下面生成的配对链接。浏览器必须保持运行且联网。<a href="https://github.com/Tencent/BrowserSkill" target="_blank" rel="noreferrer">查看安装说明</a></span>} />
    {error && <Alert type="error" showIcon title={error} action={<Button onClick={refresh}>重试</Button>} />}
    <Button type="primary" loading={busy} onClick={async () => {
      setBusy(true);
      try { setPairing(await localBrowserApi.pair(requestId.current)); }
      catch (e) { message.error(e instanceof Error ? e.message : '配对失败'); }
      finally { setBusy(false); }
    }}>生成配对链接</Button>
    {pairing && <Card title="将链接粘贴到你自己的扩展中">
      <Alert type="warning" title="链接包含一次性凭证，请勿分享或发送给智能体。" />
      <Input.TextArea aria-label="配对链接" value={pairing.url} readOnly autoSize className="my-3" />
      <Space><Button onClick={() => navigator.clipboard.writeText(pairing.url).then(() => message.success('已复制')).catch(() => message.error('复制失败，请手动选择链接复制'))}>复制链接</Button>
        <Button onClick={() => { setPairing(null); requestId.current = crypto.randomUUID(); }}>隐藏 / 重新配对</Button></Space>
      <p>有效期至 {new Date(pairing.expires_at).toLocaleTimeString()}</p>
    </Card>}
    {loading && <Skeleton active paragraph={{ rows: 3 }} />}
    {devices.map(d => <Card key={d.id} size="small" title={d.label} extra={<Tag color={d.online ? 'green' : 'default'}>{d.revoked ? '已撤销' : d.online ? '在线' : '离线'}</Tag>}>
      <p>设备 {d.id.slice(0, 8)} · {d.persistent_authorized ? '持续授权，直到主动撤销' : '每轮聊天单独授权'}</p>
      <p>连接凭证会自动续期；长时间离线导致凭证过期时需重新配对。</p>
      {d.persistent_authorized && <Alert type="info" className="mb-3" title="任意公开网站 · 普通操作免确认" description="当前用户在本租户的聊天可自动使用此设备。打开、点击和输入无需逐次确认；登录、验证码和支付由你接管。网页内容发送给当前智能体的模型服务。" />}
      {!d.revoked && <Space wrap>
        {d.persistent_authorized
          ? <Button danger onClick={async () => { try { await localBrowserApi.persistentAuthorization(d.id, false); await refresh(); } catch (e) { message.error(e instanceof Error ? e.message : '撤销失败'); } }}>撤销持续授权</Button>
          : <Popconfirm title="持续授权此浏览器？" description="当前用户和租户下，聊天将自动使用此设备，可访问任意公开网站并免确认点击和输入。登录、验证码、支付仍由你接管。持续有效，直到主动撤销。" onConfirm={async () => { try { await localBrowserApi.persistentAuthorization(d.id, true); await refresh(); message.success('持续授权已开启'); } catch (e) { message.error(e instanceof Error ? e.message : '授权失败'); } }}><Button type="primary">开启持续授权</Button></Popconfirm>}
        <Popconfirm title="撤销连接并停止此设备的浏览器任务？" onConfirm={async () => { await localBrowserApi.revoke(d.id); await refresh(); }}><Button danger>撤销连接</Button></Popconfirm>
      </Space>}
    </Card>)}
    {!loading && !error && devices.length === 0 && <p>尚未配对浏览器。</p>}
  </div></div>;
}
