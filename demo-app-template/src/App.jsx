import React, { useState } from 'react';

// Dev-only seeded session: a `demo_session` cookie logs the user in without credentials.
const loggedIn = () => document.cookie.split('; ').some((c) => c.startsWith('demo_session=1'));

function Login() {
  return (
    <main>
      <h1>Sign in</h1>
      <p>Please sign in to see your dashboard.</p>
    </main>
  );
}

function Profile() {
  const [saved, setSaved] = useState(false);
  return (
    <main>
      <h1>Your profile</h1>
      <p className="muted">Changes are saved to your account.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setSaved(true);
        }}
      >
        <div className="field">
          <label htmlFor="full-name" className="field-label">Full name</label>
          <input id="full-name" name="fullName" type="text" autoComplete="name" />
        </div>
        <div className="field">
          <span className="field-label">Email address</span>
          <input name="email" type="email" autoComplete="email" />
        </div>
        <button type="submit">Save profile</button>
        {saved && <p role="status">Profile saved.</p>}
      </form>
    </main>
  );
}

export default function App() {
  return (
    <>
      <header>
        <strong>Demo</strong>
        <button className="icon-btn" type="button">🔔</button>
      </header>
      {loggedIn() ? <Profile /> : <Login />}
    </>
  );
}
