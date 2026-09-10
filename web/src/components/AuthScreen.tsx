import type { FormHTMLAttributes, ReactNode } from 'react';
import { Table2 } from 'lucide-react';
import { useInstanceIdentity } from '../instanceIdentity';
import styles from './SignIn.module.css';

export function AuthScreen({
  children,
  heading,
  headingId,
  testId,
}: {
  children: ReactNode;
  heading: string;
  headingId: string;
  testId?: string;
}) {
  const instance = useInstanceIdentity();
  return (
    <main className={styles.screen} data-testid={testId}>
      <section className={styles.card} aria-labelledby={headingId}>
        <div className={styles.brand}>
          <Table2 size={20} strokeWidth={2.2} aria-hidden="true" />
          <span className="brand-name">{instance.display_name}</span>
        </div>
        <h1 id={headingId}>{heading}</h1>
        {children}
      </section>
    </main>
  );
}

export function AuthForm({ className, ...props }: FormHTMLAttributes<HTMLFormElement>) {
  const classes = className ? `${styles.form} ${className}` : styles.form;
  return <form className={classes} {...props} />;
}
