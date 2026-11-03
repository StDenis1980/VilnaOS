// SPDX-License-Identifier: GPL-3.0-or-later
//! Accounts from `/etc/passwd`: who a uid is and where their home is.
//!
//! The people and Gloxi users of a Vilna system are local accounts, so the
//! file is enough; accounts from a directory service are not supported.

use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Account {
    pub name: String,
    pub uid: u32,
    pub home: PathBuf,
}

/// The accounts in a passwd file's text; malformed lines are skipped.
pub fn parse(text: &str) -> Vec<Account> {
    text.lines()
        .filter(|l| !l.starts_with('#'))
        .filter_map(|line| {
            let f: Vec<&str> = line.split(':').collect();
            if f.len() != 7 || f[0].is_empty() {
                return None;
            }
            let home = PathBuf::from(f[5]);
            Some(Account {
                name: f[0].to_owned(),
                uid: f[2].parse().ok()?,
                home: home.is_absolute().then_some(home)?,
            })
        })
        .collect()
}

pub fn load(path: &Path) -> Result<Vec<Account>, String> {
    std::fs::read_to_string(path)
        .map(|t| parse(&t))
        .map_err(|e| format!("{}: {e}", path.display()))
}

/// By login name, or by uid when `who` is a number.
pub fn find<'a>(accounts: &'a [Account], who: &str) -> Option<&'a Account> {
    match who.parse::<u32>() {
        Ok(uid) => accounts.iter().find(|a| a.uid == uid),
        Err(_) => accounts.iter().find(|a| a.name == who),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_passwd() {
        let text = "root:x:0:0:root:/root:/bin/bash\n# note\nbad line\n\
                    denys:x:1000:1000:Denys:/home/denys:/bin/bash\n\
                    odd:x:abc:1:x:/home/odd:/bin/sh\nrel:x:5:5::home:/bin/sh\n";
        let all = parse(text);
        assert_eq!(all.len(), 2);
        let d = find(&all, "denys").expect("denys");
        assert_eq!((d.uid, d.home.as_path()), (1000, Path::new("/home/denys")));
        assert_eq!(find(&all, "1000"), Some(d));
        assert!(find(&all, "nobody").is_none() && find(&all, "7").is_none());
    }
}
