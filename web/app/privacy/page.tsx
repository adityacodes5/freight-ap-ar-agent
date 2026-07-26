import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy — Demo Logistics Invoice Processor",
  description: "How the Demo Logistics invoice-processing application handles data.",
};

const UPDATED = "July 7, 2026";
const CONTACT = "accounts@demologistics.com";

export default function PrivacyPolicy() {
  return (
    <main className="min-h-screen bg-white text-gray-900">
      <div className="max-w-3xl mx-auto px-6 py-12 leading-relaxed">
        <h1 className="text-3xl font-bold mb-2">Privacy Policy</h1>
        <p className="text-sm text-gray-500 mb-8">Last updated: {UPDATED}</p>

        <p className="mb-6">
          This Privacy Policy describes how the Demo Logistics Invoice Processor
          (the &ldquo;Application&rdquo;) collects, uses, and protects data. The
          Application is a private, internal tool operated by Demo Logistics Inc.
          (&ldquo;we&rdquo;, &ldquo;us&rdquo;) to automate the processing of freight
          invoices and accounting records. It is not offered to the general public.
        </p>

        <Section title="Information We Process">
          <ul className="list-disc pl-6 space-y-1">
            <li>Freight invoices and related documents received by email.</li>
            <li>Load, customer, carrier, and payment records from our accounting spreadsheets.</li>
            <li>
              Accounting data in QuickBooks Online (customers, vendors, invoices, and
              bills) that we create or read through Intuit&rsquo;s authorized API.
            </li>
          </ul>
        </Section>

        <Section title="How We Use It">
          <p>
            Data is used solely to record invoices and bills, reconcile payments,
            and maintain our own books. We do not sell, rent, or share this data with
            third parties, and we do not use it for advertising or profiling.
          </p>
        </Section>

        <Section title="QuickBooks / Intuit Data">
          <p>
            When connected to QuickBooks Online, the Application accesses only the
            accounting data required to create and reconcile invoices and bills, using
            the permissions you grant during authorization. You may revoke this access
            at any time from your Intuit account settings, after which the Application
            can no longer access your QuickBooks data.
          </p>
        </Section>

        <Section title="Data Storage & Security">
          <p>
            Access tokens and credentials are stored as protected environment
            variables and are never shared or committed to source control. Data is
            transmitted over encrypted (HTTPS) connections. Access to the Application
            is restricted to authorized company personnel.
          </p>
        </Section>

        <Section title="Data Retention">
          <p>
            We retain accounting records for as long as required for our business and
            legal obligations. Connection tokens are retained only while the QuickBooks
            connection is active.
          </p>
        </Section>

        <Section title="Contact">
          <p>
            Questions about this policy can be sent to{" "}
            <a className="text-blue-600 underline" href={`mailto:${CONTACT}`}>{CONTACT}</a>.
          </p>
        </Section>
      </div>
    </main>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="text-xl font-semibold mb-2">{title}</h2>
      <div className="text-gray-700">{children}</div>
    </section>
  );
}
