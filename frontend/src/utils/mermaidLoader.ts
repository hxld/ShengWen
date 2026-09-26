import type { Mermaid } from 'mermaid'

let mermaidInstance: Mermaid | null = null
let mermaidPromise: Promise<Mermaid> | null = null

const customTheme = {
  primaryColor: '#dbeafe',
  primaryTextColor: '#0f172a',
  primaryBorderColor: '#3b82f6',
  lineColor: '#334155',
  secondaryColor: '#f8fafc',
  tertiaryColor: '#eff6ff',
  clusterBkg: '#f8fafc',
  clusterBorder: '#cbd5e1',
  edgeLabelBackground: '#ffffff',
  fontFamily: "'Microsoft YaHei', 'Segoe UI', 'Consolas', sans-serif",
}

export async function getMermaid(): Promise<Mermaid> {
  if (mermaidInstance) {
    return mermaidInstance
  }

  if (mermaidPromise) {
    return mermaidPromise
  }

  mermaidPromise = (async () => {
    const mermaidModule = await import('mermaid')
    const mermaid = mermaidModule.default

    mermaid.initialize({
      startOnLoad: false,
      suppressErrorRendering: true,
      theme: 'base',
      themeVariables: customTheme,
      securityLevel: 'strict',
      flowchart: {
        htmlLabels: false,
        curve: 'basis',
      },
      themeCSS: `
        .mermaid .node rect, .mermaid .node circle, .mermaid .node ellipse, .mermaid .node polygon, .mermaid .node path {
          fill: #dbeafe;
          stroke: #3b82f6;
          stroke-width: 2px;
          rx: 10px;
          ry: 10px;
        }
        .mermaid .node:hover rect, .mermaid .node:hover circle, .mermaid .node:hover ellipse, .mermaid .node:hover polygon, .mermaid .node:hover path {
          fill: #bfdbfe;
        }
        .mermaid .edgePath path {
          stroke: #334155;
          stroke-width: 2px;
          fill: none;
        }
        .mermaid .cluster rect {
          fill: #f8fafc;
          stroke: #cbd5e1;
          stroke-width: 2px;
          stroke-dasharray: 5, 5;
          rx: 12px;
        }
        .mermaid text {
          fill: #0f172a;
          font-family: 'Microsoft YaHei', 'Segoe UI', 'Consolas', sans-serif;
          font-weight: 500;
        }
        .mermaid .label {
          color: #0f172a;
        }
        .mermaid .error-icon {
          fill: #dc2626;
        }
        .mermaid .error-text {
          fill: #7f1d1d;
          stroke: #7f1d1d;
        }
        .mermaid .error-message {
          color: #7f1d1d;
        }
      `
    })

    mermaidInstance = mermaid
    return mermaid
  })()

  return mermaidPromise
}
