//! Integration between Rust collection and pytest execution.

use crate::collection::error::{CollectionError, CollectionWarning};
use crate::collection::nodes::Session;
use crate::collection::types::CollectionNode;
use crate::python_discovery::DiscoveryCache;
#[cfg(any(feature = "extension-module", test))]
use std::collections::HashSet;
#[cfg(feature = "extension-module")]
use std::io::{self, Write};
use std::path::PathBuf;

/// Preserve discovery order while delegating all parameter IDs to pytest.
#[cfg(any(feature = "extension-module", test))]
pub(crate) fn pytest_function_selectors(nodes: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut selectors = Vec::new();
    for mut node in nodes {
        if let Some(separator) = node.find("::") {
            if let Some(parameter) = node[separator..].find('[') {
                node.truncate(separator + parameter);
            }
        }
        if seen.insert(node.clone()) {
            selectors.push(node);
        }
    }
    selectors
}

#[cfg(test)]
mod selector_tests {
    use super::pytest_function_selectors;

    #[test]
    fn delegates_parameters_once_without_changing_function_order() {
        let nodes = [
            "tests/test_api.py::test_request[first]",
            "tests/test_api.py::test_plain",
            "tests/test_api.py::test_request[second]",
            "tests/test_api.py::test_request",
            "tests/test_api.py::Other::test_request[first]",
        ];
        assert_eq!(
            pytest_function_selectors(nodes.map(String::from).into()),
            [
                "tests/test_api.py::test_request",
                "tests/test_api.py::test_plain",
                "tests/test_api.py::Other::test_request",
            ]
        );
    }

    #[test]
    fn preserves_brackets_in_paths_and_handles_unicode_and_nested_parameter_text() {
        let nodes = [
            "tests/[archive]/test_é.py::TestAPI::test_request[é[1]::other]",
            "tests/[archive]/test_é.py::TestAPI::test_request[é[2]]",
            "tests/[archive]/test_é.py",
        ];
        assert_eq!(
            pytest_function_selectors(nodes.map(String::from).into()),
            [
                "tests/[archive]/test_é.py::TestAPI::test_request",
                "tests/[archive]/test_é.py",
            ]
        );
    }
}

/// Holds errors and warnings encountered during collection
#[derive(Debug)]
pub struct CollectionErrors {
    pub errors: Vec<(String, CollectionError)>,
    pub warnings: Vec<CollectionWarning>,
}

#[cfg(feature = "extension-module")]
impl CollectionErrors {
    pub(crate) fn write_cannot_expand_report(&self, writer: &mut dyn Write) -> io::Result<()> {
        self.warnings
            .iter()
            .filter_map(|warning| warning.cannot_expand_nodeid.as_deref())
            .try_for_each(|nodeid| writeln!(writer, "rtest-cannot-expand: {nodeid}"))?;
        writer.flush()
    }
}

/// Run the Rust-based collection and return test node IDs
pub fn collect_tests_rust(
    rootpath: PathBuf,
    args: &[String],
) -> Result<(Vec<String>, CollectionErrors), CollectionError> {
    let session = Session::new(rootpath);
    let mut collection_errors = CollectionErrors {
        errors: Vec::new(),
        warnings: Vec::new(),
    };

    let collectors = session.perform_collect(args)?;
    let mut test_nodes = Vec::new();
    let mut discovery = DiscoveryCache::default();
    for collector in collectors {
        collect_items_recursive(
            &session,
            collector,
            &mut test_nodes,
            &mut collection_errors,
            &mut discovery,
        );
    }
    Ok((test_nodes, collection_errors))
}

/// Recursively collect all test items
fn collect_items_recursive(
    session: &Session,
    node: CollectionNode,
    test_nodes: &mut Vec<String>,
    collection_errors: &mut CollectionErrors,
    discovery: &mut DiscoveryCache,
) {
    match node {
        CollectionNode::Test(nodeid) => test_nodes.push(nodeid),
        CollectionNode::Branch(collector) => match collector.collect(session, discovery) {
            Ok((children, warnings)) => {
                collection_errors.warnings.extend(warnings);
                for child in children {
                    collect_items_recursive(
                        session,
                        child,
                        test_nodes,
                        collection_errors,
                        discovery,
                    );
                }
            }
            Err(error) => collection_errors
                .errors
                .push((collector.nodeid(session), error)),
        },
    }
}

/// Display collection results in a format similar to pytest
pub fn display_collection_results(test_nodes: &[String], errors: &CollectionErrors) {
    // ANSI color codes
    const RED: &str = "\x1b[31m";
    const BOLD_RED: &str = "\x1b[1;31m";
    const YELLOW: &str = "\x1b[33m";
    const RESET: &str = "\x1b[0m";

    if !errors.errors.is_empty() {
        println!(
            "===================================== ERRORS ======================================"
        );
        for (nodeid, error) in &errors.errors {
            println!("{BOLD_RED}_ ERROR collecting {nodeid} _{RESET}");
            match error {
                CollectionError::ParseError(msg) => {
                    println!("{RED}E   {msg}{RESET}");
                }
                CollectionError::ImportError(msg) => {
                    println!("{RED}E   ImportError: {msg}{RESET}");
                }
                CollectionError::IoError(e) => {
                    println!("{RED}E   IO Error: {e}{RESET}");
                }
                CollectionError::FileNotFound(path) => {
                    println!(
                        "{RED}E   file or directory not found: {}{RESET}",
                        path.display()
                    );
                }
            }
        }
        println!(
            "!!!!!!!!!!!!!!!!!!!!! Warning: {} errors during collection !!!!!!!!!!!!!!!!!!!!!",
            errors.errors.len()
        );
    }

    let item_count = test_nodes.len();
    let error_count = errors.errors.len();
    let warning_count = errors.warnings.len();

    if item_count == 0 && error_count == 0 {
        println!("No tests collected.");
    } else {
        let summary_parts: Vec<String> = [
            (item_count, "collected ", "item"),
            (error_count, "", "error"),
            (warning_count, "", "warning"),
        ]
        .into_iter()
        .filter(|(count, _, _)| *count > 0)
        .map(|(count, prefix, label)| {
            format!(
                "{prefix}{count} {label}{}",
                if count == 1 { "" } else { "s" }
            )
        })
        .collect();

        println!("{}", summary_parts.join(" / "));

        if !test_nodes.is_empty() {
            println!();
            for node in test_nodes {
                println!("  {node}");
            }
        }
    }

    // Display warnings after the test list
    if !errors.warnings.is_empty() {
        println!();
        println!(
            "=============================== warnings summary ==============================="
        );
        for warning in &errors.warnings {
            println!("{YELLOW}{warning}{RESET}");
        }
        println!("-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html");
    }
}
