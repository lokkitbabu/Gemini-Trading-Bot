import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Prediction Arb Dashboard',
  description: 'Real-time prediction arbitrage monitoring dashboard',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
