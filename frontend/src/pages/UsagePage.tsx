import { useEffect, useMemo, useState } from 'react';
import { Bar, Column, Line, Pie } from '@ant-design/plots';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Radio,
  Row,
  Segmented,
  Skeleton,
  Spin,
  Statistic,
  Table,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import {
  analyticsApi,
  type AnalyticsDetailItem,
  type AnalyticsDistributionItem,
  type AnalyticsScope,
  type AnalyticsSummary,
  type AnalyticsTrendPoint,
} from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';
import { useThemeStore } from '@/stores/themeStore';

const { Title } = Typography;
const { RangePicker } = DatePicker;

type Preset = 'today' | '7d' | '30d' | 'custom';

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function rangeOf(preset: Preset, custom: [Dayjs, Dayjs] | null): [string, string] {
  const today = dayjs();
  if (preset === '7d') return [today.subtract(6, 'day').format('YYYY-MM-DD'), today.format('YYYY-MM-DD')];
  if (preset === '30d') return [today.subtract(29, 'day').format('YYYY-MM-DD'), today.format('YYYY-MM-DD')];
  if (preset === 'custom' && custom) return [custom[0].format('YYYY-MM-DD'), custom[1].format('YYYY-MM-DD')];
  return [today.format('YYYY-MM-DD'), today.format('YYYY-MM-DD')];
}

function prevDelta(cur: number, prev: number): string | null {
  if (prev <= 0) return null;
  const pct = ((cur - prev) / prev) * 100;
  const sign = pct >= 0 ? '+' : '';
  return `${sign}${pct.toFixed(0)}% 较上周期`;
}

export default function UsagePage() {
  const role = useAuthStore((s) => s.role);
  const isDark = useThemeStore((s) => s.resolvedTheme()) === 'dark';
  const chartTheme = isDark ? 'dark' : undefined;
  const isSuperAdmin = role === 'superadmin';

  const [scope, setScope] = useState<AnalyticsScope>('mine');
  const [preset, setPreset] = useState<Preset>('today');
  const [customRange, setCustomRange] = useState<[Dayjs, Dayjs] | null>(null);

  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [retry, setRetry] = useState(0);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [trend, setTrend] = useState<AnalyticsTrendPoint[]>([]);
  const [modelDist, setModelDist] = useState<AnalyticsDistributionItem[]>([]);
  const [agentDist, setAgentDist] = useState<AnalyticsDistributionItem[]>([]);
  const [userDist, setUserDist] = useState<AnalyticsDistributionItem[]>([]);
  const [detail, setDetail] = useState<{ items: AnalyticsDetailItem[]; total: number }>({
    items: [],
    total: 0,
  });
  const [detailPage, setDetailPage] = useState(1);
  const [detailLoading, setDetailLoading] = useState(false);

  const [start, end] = rangeOf(preset, customRange);
  const query = useMemo(() => ({ start, end, scope }), [start, end, scope]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setSummary(null);
    setTrend([]);
    setModelDist([]);
    setAgentDist([]);
    setUserDist([]);
    setUpdatedAt(null);
    Promise.all([
      analyticsApi.summary(query), analyticsApi.trend(query),
      analyticsApi.distribution({ ...query, by: 'model' }),
      analyticsApi.distribution({ ...query, by: 'agent' }),
      scope === 'global' ? analyticsApi.distribution({ ...query, by: 'user' }) : Promise.resolve([]),
    ]).then(([summaryData, trendData, models, agents, users]) => {
      if (cancelled) return;
      setSummary(summaryData); setTrend(trendData); setModelDist(models);
      setAgentDist(agents); setUserDist(users); setUpdatedAt(new Date());
    }).catch(() => { if (!cancelled) setError('用量数据加载失败，请重试。'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [query, scope, retry]);

  useEffect(() => { setDetailPage(1); }, [query]);
  useEffect(() => {
    let cancelled = false;
    setDetailLoading(true);
    setDetailError('');
    setDetail({ items: [], total: 0 });
    analyticsApi.detail({ ...query, page: detailPage, page_size: 10 })
      .then(value => { if (!cancelled) setDetail(value); })
      .catch(() => { if (!cancelled) setDetailError('用量明细加载失败，请重试。'); })
      .finally(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [query, detailPage, retry]);

  const tokenLineData = useMemo(
    () =>
      trend.flatMap((p) => [
        { date: p.date, type: '输入', tokens: p.prompt_tokens },
        { date: p.date, type: '输出', tokens: p.completion_tokens },
      ]),
    [trend],
  );

  const detailColumns: ColumnsType<AnalyticsDetailItem> = [
    { title: '日期', dataIndex: 'date', key: 'date' },
    { title: '模型', dataIndex: 'model', key: 'model' },
    { title: '输入 Tokens', dataIndex: 'prompt_tokens', key: 'prompt', align: 'right', render: (v: number) => v.toLocaleString() },
    { title: '输出 Tokens', dataIndex: 'completion_tokens', key: 'completion', align: 'right', render: (v: number) => v.toLocaleString() },
    { title: '总 Tokens', dataIndex: 'total_tokens', key: 'total', align: 'right', render: (v: number) => v.toLocaleString() },
    { title: '请求次数', dataIndex: 'request_count', key: 'requests', align: 'right', render: (v: number) => v.toLocaleString() },
  ];

  const cards: { title: string; value: number; prev: number }[] = summary
    ? [
        { title: '会话数', value: summary.sessions, prev: summary.prev_sessions },
        { title: '消息数', value: summary.messages, prev: summary.prev_messages },
        { title: 'Token 总量', value: summary.total_tokens, prev: summary.prev_total_tokens },
        { title: '请求次数', value: summary.request_count, prev: summary.prev_request_count },
      ]
    : [];

  return (
    <div className="h-full overflow-y-auto">
      <div className="page-content max-w-[1200px] mx-auto">
      {error && <Alert className="mb-4" type="error" showIcon title={error} action={<Button onClick={() => setRetry(value => value + 1)}>重试</Button>} />}
      {updatedAt && <p className="mb-2 text-xs text-muted-foreground">更新于 {updatedAt.toLocaleTimeString()}</p>}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>用量统计</Title>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          {isSuperAdmin && (
            <Segmented
              value={scope}
              onChange={(v) => setScope(v as AnalyticsScope)}
              options={[
                { label: '我的', value: 'mine' },
                { label: '全局', value: 'global' },
              ]}
            />
          )}
          <Radio.Group
            value={preset}
            onChange={(e) => setPreset(e.target.value as Preset)}
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value="today">今天</Radio.Button>
            <Radio.Button value="7d">近 7 天</Radio.Button>
            <Radio.Button value="30d">近 30 天</Radio.Button>
          </Radio.Group>
          <RangePicker
            value={customRange}
            onChange={(v) => {
              setCustomRange(v as [Dayjs, Dayjs] | null);
              if (v) setPreset('custom');
            }}
            allowClear
          />
        </div>
      </div>

      {loading && !summary ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : (
        <Spin spinning={loading}>
          <Row gutter={[16, 16]}>
            {cards.map((c) => (
              <Col xs={12} md={6} key={c.title}>
                <Card>
                  <Statistic title={c.title} value={c.value} formatter={() => fmt(c.value)} />
                  <div style={{ fontSize: 12, color: '#888', minHeight: 18 }}>
                    {prevDelta(c.value, c.prev) ?? ''}
                  </div>
                </Card>
              </Col>
            ))}
            {scope === 'global' && summary?.active_users != null && (
              <Col xs={12} md={6}>
                <Card>
                  <Statistic title="活跃用户" value={summary.active_users} />
                  <div style={{ fontSize: 12, minHeight: 18 }} />
                </Card>
              </Col>
            )}
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <Card title="Token 趋势" size="small">
                <Line data={tokenLineData} xField="date" yField="tokens" colorField="type" height={260} theme={chartTheme} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="会话数趋势" size="small">
                <Column data={trend} xField="date" yField="sessions" height={260} theme={chartTheme} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="模型分布（Token）" size="small">
                <Pie
                  data={modelDist}
                  angleField="total_tokens"
                  colorField="label"
                  innerRadius={0.5}
                  height={280}
                  theme={chartTheme}
                  legend={{ color: { position: 'bottom' } }}
                />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              {scope === 'global' ? (
                <Card title="用户排行 Top 20（Token）" size="small">
                  <Bar data={userDist} xField="total_tokens" yField="label" height={280} theme={chartTheme} />
                </Card>
              ) : (
                <Card title="智能体分布（会话数）" size="small">
                  <Bar data={agentDist} xField="sessions" yField="label" height={280} theme={chartTheme} />
                </Card>
              )}
            </Col>
          </Row>

          <Card title="用量明细" size="small" style={{ marginTop: 16 }}>
            {detailError && <Alert className="mb-3" type="error" showIcon title={detailError} action={<Button onClick={() => setRetry(value => value + 1)}>重试</Button>} />}
            <Table
              scroll={{ x: 720 }}
              rowKey={(r) => `${r.date}-${r.model}`}
              loading={detailLoading}
              columns={detailColumns}
              dataSource={detail.items}
              size="small"
              pagination={{
                current: detailPage,
                total: detail.total,
                pageSize: 10,
                showSizeChanger: false,
                onChange: (p) => {
                  setDetailPage(p);
                },
              }}
            />
          </Card>
        </Spin>
      )}
      </div>
    </div>
  );
}
