import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'FinSight AI — Corporate Finance Autopilot',
  description: 'Agentic equity research. Ingest → Model → Brief.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}