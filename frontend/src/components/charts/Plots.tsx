import { lazy, Suspense, type ComponentProps } from 'react';

// Charts are optional panels. Keep the page header, controls and trace table
// available while the plotting runtime loads.
const BarPlot = lazy(() => import('@ant-design/plots').then(plots => ({ default: plots.Bar })));
const ColumnPlot = lazy(() => import('@ant-design/plots').then(plots => ({ default: plots.Column })));
const LinePlot = lazy(() => import('@ant-design/plots').then(plots => ({ default: plots.Line })));
const PiePlot = lazy(() => import('@ant-design/plots').then(plots => ({ default: plots.Pie })));

function Placeholder({ height }: { height?: number }) {
  return <div role="status" className="flex items-center justify-center text-muted-foreground" style={{ height }}>图表加载中…</div>;
}

export function Bar(props: ComponentProps<typeof BarPlot>) {
  return <Suspense fallback={<Placeholder height={props.height} />}><BarPlot {...props} /></Suspense>;
}

export function Column(props: ComponentProps<typeof ColumnPlot>) {
  return <Suspense fallback={<Placeholder height={props.height} />}><ColumnPlot {...props} /></Suspense>;
}

export function Line(props: ComponentProps<typeof LinePlot>) {
  return <Suspense fallback={<Placeholder height={props.height} />}><LinePlot {...props} /></Suspense>;
}

export function Pie(props: ComponentProps<typeof PiePlot>) {
  return <Suspense fallback={<Placeholder height={props.height} />}><PiePlot {...props} /></Suspense>;
}
