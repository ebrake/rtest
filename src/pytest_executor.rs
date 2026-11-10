//! Handles the execution of pytest with collected test nodes.

use std::io::{self, Write};
use std::path::Path;
use std::process::Command;
use tempfile::NamedTempFile;

pub(crate) struct PreparedPytestCommand {
    pub command: Command,
    // The child must finish reading its selectors before this file is removed.
    _selector_file: Option<NamedTempFile>,
}

pub(crate) fn pytest_command(
    program: &str,
    initial_args: &[String],
    test_nodes: &[String],
    pytest_args: &[String],
    working_dir: Option<&Path>,
    env_vars: &[(String, String)],
) -> io::Result<PreparedPytestCommand> {
    let mut command = Command::new(program);
    command
        .args(initial_args)
        .envs(env_vars.iter().map(|(key, value)| (key, value)));

    // Keep pytest from searching parent directories for configuration, including
    // protected system directories on Windows.
    if let Some(dir) = working_dir {
        command.current_dir(dir).arg("--rootdir").arg(dir);
    }

    let selector_file = if test_nodes.iter().map(|node| node.len() + 1).sum::<usize>() > 32_768 {
        let mut file = NamedTempFile::new()?;
        for node in test_nodes {
            if node.contains(['\n', '\r']) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "pytest argument files cannot represent selectors containing line breaks",
                ));
            }
            writeln!(file, "{node}")?;
        }
        file.flush()?;
        command.arg(format!("@{}", file.path().display()));
        Some(file)
    } else {
        command.args(test_nodes);
        None
    };
    command.args(pytest_args);
    Ok(PreparedPytestCommand {
        command,
        _selector_file: selector_file,
    })
}

/// Executes pytest with the given program, initial arguments, collected test nodes, and additional pytest arguments.
///
/// # Arguments
///
/// * `program` - The pytest executable or package manager command.
/// * `initial_args` - Initial arguments to pass to the program (e.g., `run` for `uv`).
/// * `test_nodes` - A `Vec<String>` of test node IDs to execute.
/// * `pytest_args` - Additional arguments to pass directly to pytest.
/// * `working_dir` - Optional working directory for pytest execution.
/// * `env_vars` - Environment variables to set for pytest execution.
///
/// Returns the exit code from pytest.
pub fn execute_tests(
    program: &str,
    initial_args: &[String],
    test_nodes: Vec<String>,
    pytest_args: Vec<String>,
    working_dir: Option<&Path>,
    env_vars: &[(String, String)],
) -> i32 {
    let mut prepared = match pytest_command(
        program,
        initial_args,
        &test_nodes,
        &pytest_args,
        working_dir,
        env_vars,
    ) {
        Ok(prepared) => prepared,
        Err(error) => {
            log::error!("Failed to prepare pytest command: {error}");
            return 1;
        }
    };
    let run_status = match prepared.command.status() {
        Ok(status) => status,
        Err(e) => {
            log::error!("Failed to execute pytest command: {e}");
            return 1;
        }
    };

    run_status.code().unwrap_or_else(|| {
        log::warn!("Pytest process terminated by signal");
        1
    })
}

#[cfg(test)]
mod argument_file_tests {
    use super::pytest_command;

    #[test]
    fn long_selectors_are_kept_alive_until_the_prepared_command_is_dropped() {
        let nodes = vec!["tests/test_api.py::test_request".to_string(); 2_000];
        let prepared =
            pytest_command("pytest", &[], &nodes, &[], None, &[]).expect("prepare argument file");
        let file = prepared
            ._selector_file
            .as_ref()
            .expect("long selector list");
        let path = file.path().to_owned();
        assert_eq!(
            std::fs::read_to_string(&path).expect("read selectors"),
            nodes.join("\n") + "\n"
        );
        assert_eq!(prepared.command.get_args().count(), 1);
        drop(prepared);
        assert!(!path.exists());
    }

    #[test]
    fn short_lists_preserve_argument_boundaries_and_flags() {
        let nodes = vec!["tests/test_api.py::test_request[space and é]".to_string()];
        let prepared = pytest_command("pytest", &[], &nodes, &["-q".to_string()], None, &[])
            .expect("prepare direct arguments");
        assert!(prepared._selector_file.is_none());
        assert_eq!(
            prepared.command.get_args().collect::<Vec<_>>(),
            [std::ffi::OsStr::new(&nodes[0]), std::ffi::OsStr::new("-q")]
        );
    }

    #[test]
    fn rejects_multiline_selectors_instead_of_splitting_them_into_arguments() {
        let nodes = vec!["tests/test_api.py::test_request[line\nbreak]".to_string(); 2_000];
        assert!(pytest_command("pytest", &[], &nodes, &[], None, &[]).is_err());
    }
}
