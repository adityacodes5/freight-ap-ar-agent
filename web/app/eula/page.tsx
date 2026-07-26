import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "End User License Agreement — Demo Logistics Invoice Processor",
  description: "Terms governing use of the Demo Logistics invoice-processing application.",
};

const UPDATED = "July 7, 2026";
const CONTACT = "accounts@demologistics.com";

export default function Eula() {
  return (
    <main className="min-h-screen bg-white text-gray-900">
      <div className="max-w-3xl mx-auto px-6 py-12 leading-relaxed">
        <h1 className="text-3xl font-bold mb-2">End User License Agreement</h1>
        <p className="text-sm text-gray-500 mb-8">Last updated: {UPDATED}</p>

        <p className="mb-6">
          This End User License Agreement (&ldquo;Agreement&rdquo;) governs use of the
          Demo Logistics Invoice Processor (the &ldquo;Application&rdquo;), a private
          internal tool operated by Demo Logistics Inc. By using the Application, the
          user agrees to these terms.
        </p>

        <Section title="1. License">
          <p>
            The Application is provided for the internal business use of Demo
            Logistics Inc. and its authorized personnel only. No license is granted to
            any other party, and the Application may not be copied, distributed, or made
            available to third parties.
          </p>
        </Section>

        <Section title="2. Permitted Use">
          <p>
            Authorized users may use the Application to process freight invoices, create
            and reconcile accounting records, and connect to approved services such as
            QuickBooks Online, in accordance with each service&rsquo;s own terms.
          </p>
        </Section>

        <Section title="3. Third-Party Services">
          <p>
            The Application integrates with third-party services (including Intuit
            QuickBooks Online). Use of those services is subject to their respective
            terms and privacy policies. We are not responsible for third-party services.
          </p>
        </Section>

        <Section title="4. Data">
          <p>
            Handling of data is described in our{" "}
            <a className="text-blue-600 underline" href="/privacy">Privacy Policy</a>.
          </p>
        </Section>

        <Section title="5. No Warranty">
          <p>
            The Application is provided &ldquo;as is&rdquo; without warranties of any
            kind, express or implied. We do not warrant that it will be error-free or
            uninterrupted.
          </p>
        </Section>

        <Section title="6. Limitation of Liability">
          <p>
            To the maximum extent permitted by law, Demo Logistics Inc. shall not be
            liable for any indirect, incidental, or consequential damages arising from
            use of the Application.
          </p>
        </Section>

        <Section title="7. Contact">
          <p>
            Questions about this Agreement can be sent to{" "}
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
