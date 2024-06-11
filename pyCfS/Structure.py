"""
Structure.py
This file contains the functions for creating lollipop plots and analyzing the structures of proteins.
"""

import os
conda_env_name = os.environ.get('CONDA_DEFAULT_ENV')
r_path = os.path.join(os.environ['CONDA_PREFIX'], 'lib', 'R')
r_libs_path = os.path.join(os.environ['CONDA_PREFIX'], 'lib', 'R', 'library')
os.environ['R_HOME'] = r_path

import tempfile
from IPython.display import Image
import rpy2 # type: ignore
from rpy2.robjects.packages import importr, isinstalled # type: ignore
import rpy2.robjects as robjects # type: ignore
from rpy2.robjects import r, pandas2ri, globalenv # type: ignore
from rpy2.robjects.conversion import localconverter # type: ignore
from rpy2.robjects.vectors import StrVector # type: ignore
import rpy2.rinterface_lib.callbacks # type: ignore
import logging
import pandas as pd
import urllib.request
import numpy as np
import time
import statsmodels.api as sm
import warnings
import pkg_resources
from io import BytesIO
import seaborn as sns
import matplotlib.pyplot as plt
from PIL import Image as PILImage
from .utils import _fix_savepath, _filter_variants, _clean_variant_formats, _load_pdb_et_mapping, _check_ensp_len

warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message="invalid value encountered in log")
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message="invalid value encountered in sqrt")
rpy2.rinterface_lib.callbacks.logger.setLevel(logging.ERROR)


#region Lollipop Plots
def _fishers_exact_test(case_vars: pd.DataFrame, cont_vars: pd.DataFrame, case_pop:int, cont_pop:int) -> (float, float, float, float): # type: ignore
    """
    Perform Fisher's exact test to calculate odds ratio, confidence interval, and p-value.

    Args:
        case_vars (pd.DataFrame): DataFrame containing case variables.
        cont_vars (pd.DataFrame): DataFrame containing control variables.
        case_pop (int): Total population of cases.
        cont_pop (int): Total population of controls.

    Returns:
        tuple: A tuple containing the odds ratio, lower confidence interval, upper confidence interval, and p-value.
    """
    # Create contingency table
    obs_case_alleles = case_vars['zyg'].sum()
    obs_cont_alleles = cont_vars['zyg'].sum()
    exp_case_alleles = case_pop - obs_case_alleles
    exp_cont_alleles = cont_pop - obs_cont_alleles
    contingency_table = [[obs_case_alleles, obs_cont_alleles], [exp_case_alleles, exp_cont_alleles]]
    # Perform Fisher's exact test
    oddsratio_data = sm.stats.Table2x2(contingency_table)
    odds_ratio = oddsratio_data.oddsratio
    lower_ci, upper_ci = oddsratio_data.oddsratio_confint()
    pval = oddsratio_data.oddsratio_pvalue()
    return odds_ratio, lower_ci, upper_ci, pval

def _r_install_package(package_name:str) -> None:
    """
    Attempts to install an R package and its dependencies.

    Parameters:
    - package_name: The name of the package to install.
    """
    # Define the CRAN repository URL
    utils = importr('utils')
    utils.chooseCRANmirror(ind=1)
    os.environ['R_REMOTES_NO_ERRORS_FROM_WARNINGS'] = 'true'
    os.environ['R_REMOTES_UPGRADE'] = "never"

    # Install the package if it is not already installed
    if not isinstalled(package_name):
        try:
            if package_name == 'EvoTrace':
                remote = importr('remotes')
                remote.install_github('LichtargeLab/EvoTrace', build_vignettes=False)
            else:
                utils.install_packages(StrVector([package_name]))
        except rpy2.rinterface_lib.embedded.RRuntimeError as e:
            print(f"An error occurred while installing {package_name}: {e}")
            # Attempt to install system dependencies if the package is known to require compilation
            if package_name in ['interp']:
                print(f"Attempting to install system dependencies for {package_name}")
                try:
                    utils.install_packages(StrVector([package_name]))
                except Exception as e:
                    print(f"Failed to install {package_name} after attempting to resolve dependencies: {e}")

def _install_evotrace() -> None:
    """
    Installs EvoTrace package.
    """
    # Set CRAN mirror
    cran_mirror = "https://cran.rstudio.com"
    robjects.r.options(repos=cran_mirror)
    # Install R packages
    _r_install_package('remotes')
    _r_install_package('EvoTrace')

def _check_pdb_id(protein_id:str) -> str:
    """
    Checks if the given protein ID exists in the PDB mapping dataframe.

    Parameters:
    protein_id (str): The protein ID to be checked.

    Returns:
    str: The validated protein ID.

    Raises:
    ValueError: If the protein ID is not found in the PDB mapping.
    """
    # Load pdb mapping dataframe
    pdb_df = _load_pdb_et_mapping()
    # Check if protein ID in dataframe
    if protein_id not in pdb_df['prot_id'].values:
        protein_id = protein_id.split('.')[0]
        pdb_sub = pdb_df[pdb_df['prot_id'].str.contains(protein_id)]
        if pdb_sub.shape[0] == 0:
            raise ValueError("Protein ID not found in PDB mapping.")
        protein_id = pdb_sub['prot_id'].values[0]
    return protein_id

def _r_lollipop_plot2(case_vars: pd.DataFrame, cont_vars: pd.DataFrame, plot_domain:bool, ac_scale:str, ea_color:str, domain_min_dist:int) -> PILImage.Image:
    """
    Generate a lollipop plot using R's EvoTrace package.

    Args:
        case_vars (pd.DataFrame): DataFrame containing case variants.
        cont_vars (pd.DataFrame): DataFrame containing control variants.
        plot_domain (bool): Flag indicating whether to plot the protein domain.
        ac_scale (str): Scale for the allele count axis. Must be one of 'linear' or 'log'.
        ea_color (str): Color scheme for the effect allele. Must be one of 'prismatic', 'gray_scale', 'EA_bin', or 'black'.
        domain_min_dist (int): Minimum distance between two domains.

    Returns:
        PIL.Image.Image: The generated lollipop plot as a PIL Image object.
    """
    # Install EvoTrace package
    _install_evotrace()
    evotrace = importr('EvoTrace')
    # Convert local DataFrames to R DataFrames
    with localconverter(robjects.default_converter + pandas2ri.converter):
        r_case_vars = robjects.conversion.py2rpy(case_vars)
        r_cont_vars = robjects.conversion.py2rpy(cont_vars)
    # Check options
    ensp_hold = _check_ensp_len(case_vars.ENSP.unique().tolist())
    prot_id = case_vars.loc[0, 'ENSP']
    prot_id = _check_pdb_id(prot_id)
    if ea_color not in ['prismatic', 'gray_scale', 'EA_bin', 'black']:
        raise ValueError("ea_color must be one of 'prismatic', 'gray_scale', 'EA_bin', or 'black'")
    if ac_scale not in ['linear', 'log']:
        raise ValueError("ac_scale must be one of 'linear' or 'log'")
    # Create temporary directory
    temp_file = tempfile.NamedTemporaryFile(delete = False, suffix = ".png")
    plot_path = temp_file.name
    # Create the lollipop plot
    try: globalenv['plot'] = evotrace.LollipopPlot2(
        variants_case = r_case_vars,
        variants_ctrl = r_cont_vars,
        prot_id = prot_id,
        plot_domain = plot_domain,
        AC_scale = ac_scale,
        show_EA_bin = True,
        fix_scale = True,
        EA_color = ea_color,
        domain_min_dist = domain_min_dist
    )
    except Exception as e:
        if "missing value where TRUE/FALSE needed" in str(e):
            print(f'ENSP ID {ensp_hold[0]} not found in PDB mapping, trying {ensp_hold[1]}')
            try: globalenv['plot'] = evotrace.LollipopPlot2(
                    variants_case = r_case_vars,
                    variants_ctrl = r_cont_vars,
                    prot_id = ensp_hold[1],
                    plot_domain = plot_domain,
                    AC_scale = ac_scale,
                    show_EA_bin = True,
                    fix_scale = True,
                    EA_color = ea_color,
                    domain_min_dist = domain_min_dist
                )
            except Exception as e:
                print(f"RRuntimeError::{e}")
                return PILImage.new('RGB', (1, 1))
        else:
            print(f"RRuntimeError::{e}")
            return PILImage.new('RGB', (1, 1))

    r(f"""ggsave("{plot_path}", plot, device = "png", width = 10, height = 5, dpi = 300)""")
    # Display the plot
    lollipop_plot_plot = Image(filename=plot_path)
    os.unlink(plot_path)
    temp_file.close()
    image_buffer = BytesIO(lollipop_plot_plot.data)
    lollipop_plot_plot = PILImage.open(image_buffer)
    return lollipop_plot_plot

def _r_lollipop_plot1(input_vars: pd.DataFrame, plot_domain:bool, ac_scale:str, ea_color:str, domain_min_dist:int) -> PILImage.Image:
    """
    Generate a lollipop plot using the EvoTrace package.

    Args:
        input_vars (pd.DataFrame): A DataFrame containing input variables.
        plot_domain (bool): Whether to plot the domain.
        ac_scale (str): The scale for the AC (Allele Count) axis. Must be one of 'linear' or 'log'.
        ea_color (str): The color scheme for the EA (Effect Allele) axis. Must be one of 'prismatic', 'gray_scale', 'EA_bin', or 'black'.
        domain_min_dist (int): The minimum distance between two domains.

    Returns:
        PILImage.Image: The generated lollipop plot as a PIL Image object.
    """
    # Install EvoTrace package
    _install_evotrace()
    evotrace = importr('EvoTrace')
    # Convert local DataFrames to R DataFrames
    with localconverter(robjects.default_converter + pandas2ri.converter):
        r_input_vars = robjects.conversion.py2rpy(input_vars)
    # Check options
    ensp_hold = _check_ensp_len(input_vars.ENSP.unique())
    prot_id = _check_pdb_id(ensp_hold[0])

    # Check values
    if ea_color not in ['prismatic', 'gray_scale', 'EA_bin', 'black']:
        raise ValueError("ea_color must be one of 'prismatic', 'gray_scale', 'EA_bin', or 'black'")
    if ac_scale not in ['linear', 'log']:
        raise ValueError("ac_scale must be one of 'linear' or 'log'")

    # Create temporary directory
    temp_file = tempfile.NamedTemporaryFile(delete = False, suffix = ".png")
    plot_path = temp_file.name

    # Create the lollipop plot
    try: globalenv['plot'] = evotrace.LollipopPlot(
        variants = r_input_vars,
        prot_id = prot_id,
        plot_domain = plot_domain,
        AC_scale = ac_scale,
        show_EA_bin = True,
        fix_scale = True,
        EA_color = ea_color,
        domain_min_dist = domain_min_dist
    )
    except Exception as e:
        if "missing value where TRUE/FALSE needed" in str(e):
            print(f'ENSP ID {ensp_hold[0]} does not match protein length, trying {ensp_hold[1]}')
            try: globalenv['plot'] = evotrace.LollipopPlot(
                variants = r_input_vars,
                prot_id = ensp_hold[1],
                plot_domain = plot_domain,
                AC_scale = ac_scale,
                show_EA_bin = True,
                fix_scale = True,
                EA_color = ea_color,
                domain_min_dist = domain_min_dist
            )
            except Exception as e:
                print(f"RRuntimeError::{e}")
                return PILImage.new('RGB', (1, 1))
        else:
            print(f"RRuntimeError::{e}")
            return PILImage.new('RGB', (1, 1))
    # Save the plot
    r(f"""ggsave("{plot_path}", plot, device = "png", width = 10, height = 5, dpi = 300)""")
    # Display the plot
    lollipop_plot_plot = Image(filename=plot_path)
    os.unlink(plot_path)
    temp_file.close()
    image_buffer = BytesIO(lollipop_plot_plot.data)
    lollipop_plot_plot = PILImage.open(image_buffer)
    return lollipop_plot_plot

def lollipop_plot(variants: pd.DataFrame, gene: str, group:str = 'both', case_pop:int=0, cont_pop:int=0, max_af:float = 1.0, min_af:float = 0.0, ea_lower:float = 0.0, ea_upper:float = 100.0, show_domains:bool = True, ac_scale:str = 'linear', ea_color:str = 'prismatic', domain_min_dist:int = 20, savepath:str = False) -> (Image, float, float, float, float): # type: ignore
    """
    Generate a lollipop plot for a given gene based on variant data.

    Parameters:
    - variants (pd.DataFrame): DataFrame containing variant data.
    - gene (str): Gene of interest.
    - case_pop (int, optional): Number of cases in the variant file. If not provided, it will be calculated based on the data.
    - cont_pop (int, optional): Number of controls in the variant file. If not provided, it will be calculated based on the data.
    - max_af (float, optional): Maximum allele frequency threshold for filtering variants. Default is 1.0.
    - ea_lower (float, optional): Lower bound for effect size threshold. Default is 0.0.
    - ea_upper (float, optional): Upper bound for effect size threshold. Default is 100.0.
    - show_domains (bool, optional): Whether to show domain annotations on the lollipop plot. Default is True.
    - ac_scale (str, optional): Scale for the allele count axis. Default is 'linear'.
    - ea_color (str, optional): Color scheme for effect size. Default is 'prismatic'.
    - domain_min_dist (int, optional): Minimum distance between domain annotations. Default is 20.
    - savepath (str, optional): Path to save the lollipop plot image. If not provided, the plot will not be saved.

    Returns:
    - plot (Image): Lollipop plot image.
    - pval (float): p-value from Fisher's exact test.
    - odds_ratio (float): odds ratio from Fisher's exact test.
    - lower_ci (float): lower confidence interval from Fisher's exact test.
    - upper_ci (float): upper confidence interval from Fisher's exact test.
    """
    # Check the number of samples
    if (case_pop == 0) & (cont_pop == 0):
        case_pop = variants['sample'][variants['CaseControl'] == 1].nunique()
        cont_pop = variants['sample'][variants['CaseControl'] == 0].nunique()
    print(f"Number of cases in variant file: {case_pop}")
    print(f"Number of controls in variant file: {cont_pop}")
    # Filter dataframe for gene and split by case and control
    case_vars, cont_vars = _filter_variants(variants, gene, max_af, min_af, ea_lower, ea_upper)
    # Clean variant annotations
    case_vars_collapsed, cont_vars_collapsed = _clean_variant_formats(case_vars), _clean_variant_formats(cont_vars)

    # Run lollipop_plot2 for both groups
    if group == 'both':
        # Calculate fisher's exact test
        odds_ratio, lower_ci, upper_ci, pval = _fishers_exact_test(case_vars, cont_vars, case_pop, cont_pop)
        # Create the lollipop plot
        plot = _r_lollipop_plot2(case_vars_collapsed, cont_vars_collapsed, plot_domain = show_domains, ac_scale = ac_scale, ea_color = ea_color, domain_min_dist = domain_min_dist)

    # Run lollipop_plot1 for single group
    elif group == 'case' or group == 'control':
        input_vars = case_vars_collapsed if group == 'case' else cont_vars_collapsed
        # Create lollipop plot
        plot = _r_lollipop_plot1(input_vars, plot_domain = show_domains, ac_scale = ac_scale, ea_color = ea_color, domain_min_dist = domain_min_dist)
        pval, odds_ratio, lower_ci, upper_ci = None, None, None, None

    # Save data
    if savepath:
        savepath = _fix_savepath(savepath)
        new_savepath = os.path.join(savepath, f'LollipopPlots/{gene}/')
        os.makedirs(new_savepath, exist_ok=True)
        plot.save(new_savepath + f'{gene}_{group}_EA{ea_lower}-{ea_upper}_AF{max_af}-{min_af}_lollipop_plot.png')
    # Need to group by zyg for fishers exact test
    return plot, pval, odds_ratio, lower_ci, upper_ci
#endregion


#region Protein Structure Visualization
def _r_alphafold_structure(case_vars: pd.DataFrame, cont_vars: str, savepath: str) -> None:
    """
    Calls the AlphaFold structure prediction method using R and saves the output in a specified path.

    Args:
        case_vars (pd.DataFrame): A DataFrame containing case variables.
        cont_vars (str): A string representing control variables.
        savepath (str): The path where the output will be saved.

    Returns:
        None
    """
    # Set CRAN mirror
    cran_mirror = "https://cran.rstudio.com"
    robjects.r.options(repos=cran_mirror)
    # Install R packages
    _r_install_package('remotes')
    _r_install_package('EvoTrace')
    _r_install_package('curl')
    evotrace = importr('EvoTrace')
    # Convert local DataFrames to R DataFrames
    with localconverter(robjects.default_converter + pandas2ri.converter):
        r_case_vars = robjects.conversion.py2rpy(case_vars)
        r_cont_vars = robjects.conversion.py2rpy(cont_vars)
    # Check options
    prot_id = case_vars.loc[0, 'ENSP']
    # Call the structure
    evotrace.Color_Variants_AlphaFold(
        variants_case=r_case_vars,
        variants_ctrl=r_cont_vars,
        prot_id=prot_id,
        pml_output=savepath
    )

def _prepare_resi_df(variants: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare a DataFrame containing unique residues from the given variants DataFrame.

    Args:
        variants (pd.DataFrame): A DataFrame containing variant data.

    Returns:
        pd.DataFrame: A DataFrame containing unique residues extracted from the variants DataFrame.
    """
    residues = variants['SUB'].unique().tolist()
    residues = ["".join(filter(str.isdigit, residue)) for residue in residues]
    vars_residues = pd.DataFrame({'residues': residues})
    return vars_residues

def _test_cutoff(min_cutoff:int, max_cutoff:int) -> None:
    """
    Check if the minimum cutoff is greater than the maximum cutoff.

    Parameters:
    min_cutoff (int): The minimum cutoff value.
    max_cutoff (int): The maximum cutoff value.

    Raises:
    ValueError: If the minimum cutoff is greater than the maximum cutoff.

    Returns:
    None
    """
    if min_cutoff > max_cutoff:
        raise ValueError("Minimum cutoff cannot be greater than maximum cutoff.")

def _download_af_model(url:str) -> pd.DataFrame:
    """
    Downloads a file from the given URL and returns its contents as a pandas DataFrame.

    Parameters:
    url (str): The URL of the file to be downloaded.

    Returns:
    pd.DataFrame: The contents of the downloaded file as a pandas DataFrame.
    """
    try:
        with urllib.request.urlopen(url) as response:
            text = response.read().decode('utf-8')
            lines = text.splitlines()
    except urllib.error.URLError as e:
        print(f"An error occurred while downloading the file: {e}, waiting another 60s")
        time.sleep(60)
        try:
            with urllib.request.urlopen(url) as response:
                text = response.read().decode('utf-8')
                lines = text.splitlines()
        except urllib.error.URLError as e:
            print(f"An error occurred while downloading the file: {e}, skipping")
            return pd.DataFrame()
    #df = pd.DataFrame([line.split('\t') for line in lines[1:]], columns=lines[0].split('\t'))
    return lines

def _clean_af_model(lines: list, chain:str, plddt_cutoff:int) -> pd.DataFrame:
    """
    Cleans and filters the atomic model data based on specified criteria.

    Args:
        lines (list): List of lines containing atomic model data.
        chain (str): Chain identifier to filter the data by.
        plddt_cutoff (int): Minimum pLDDT value to filter the data by.

    Returns:
        pd.DataFrame: Filtered and cleaned atomic model data.

    """
    # Filter lines that start with 'ATOM' or 'HETATM'
    atom_lines = [line for line in lines if line[:6].strip() in ['ATOM', 'HETATM']]
    # Define column widths as per PDB format specifications
    col_specs = [(0, 6), (6, 11), (12, 16), (16, 17), (17, 20), (21, 22), (22, 26), (26, 27),
                 (30, 38), (38, 46), (46, 54), (54, 60), (60, 66), (76, 78), (78, 80)]
    col_names = [
        'record', 'atom_number', 'atom_type', 'blank_1', 'residue_name', 'chain', 'residue_seq_number', 'blank_3', 'x', 'y', 'z', 'occupancy', 'temp_factor', 'element', 'charge'
    ]
    # Create DataFrame from the filtered lines
    df = pd.read_fwf(pd.io.common.StringIO('\n'.join(atom_lines)), colspecs=col_specs, names=col_names, header=None)
    # Filter DataFrame by chain and residue type
    df = df[df['record'].isin(['ATOM', 'HETATM'])]
    df = df[df['residue_name'].isin(
        [
            'HIS', 'PRO', 'GLU', 'THR', 'LEU', 'VAL', 'LYS', 'ASP', 'ALA',
            'GLN', 'GLY', 'ARG', 'TYR', 'ILE', 'ASN', 'SER', 'PHE', 'MET',
            'CYS', 'TRP', 'MSE'
        ]
    )]
    df = df[df['atom_type'].str.strip() == 'CA']
    df = df[df['chain'].str.strip() == chain]
    # Select and rename the columns
    df = df[['chain', 'residue_seq_number', 'temp_factor']]
    df.columns = ['chain', 'POS', 'pLDDT']
    df = df.sort_values(by=['chain', 'POS'])
    df = df[df['pLDDT'] >= plddt_cutoff]
    return df

def _get_plddt(prot_id:str, gene:str, chain:str, plddt_cutoff:int, savepath:str) -> pd.DataFrame:
    """
    Retrieves the pLDDT scores for a given protein and chain from the AlphaFold model.

    Args:
        prot_id (str): The protein identifier.
        chain (str): The chain identifier.
        plddt_cutoff (int): The pLDDT cutoff value.

    Returns:
        af_model_clean (DataFrame): The cleaned dataframe containing the pLDDT scores.
    """
    # Load the pdb mapping dataframe
    pdb_df = _load_pdb_et_mapping()
    af_id = pdb_df['AF_id_rep'][pdb_df['prot_id'].str.contains(prot_id)].values[0]
    # Create the save path
    if savepath:
        savepath = _fix_savepath(savepath)
        pdb_path = os.path.join(savepath, f'ProteinStructures/{gene}/{gene}_AF-{af_id}.pdb')
    if not savepath:
        temp_file = tempfile.NamedTemporaryFile(delete = False, suffix = ".pdb")
        pdb_path = temp_file.name
    # Filter for the given gene name
    try: af_url = pdb_df['AF_url'][pdb_df['prot_id'].str.contains(prot_id)].values[0]
    except:
        print("Gene not found in PDB-AF ID map.")
        return pd.DataFrame()
    # Download the AlphaFold model
    af_model = _download_af_model(af_url)
    with open(pdb_path, 'w') as f:
        f.write('\n'.join(af_model))
    if len(af_model) == 0:
        return pd.DataFrame()
    # Parse the model into cleaned dataframe
    af_model_clean = _clean_af_model(af_model, chain, plddt_cutoff)
    background_resi = af_model_clean['POS'].unique().tolist()
    return background_resi, pdb_path

def _plot_scw_z(output_df:pd.DataFrame):
    sns.lineplot(data=output_df, x='dist_cutoff', y='z_score', hue='scw_type')
    plt.axhline(y=2, color='red', linestyle='--')
    plt.xlim([args.min_cutoff - 1, args.max_cutoff + 1])

def _scw_analysis(pdb_path:str, background_resi:list, residues:pd.DataFrame, chain:str, dist_cutoff:int):
    # Install EvoTrace
    _r_install_package('EvoTrace')
    evotrace = importr('EvoTrace')

    # Convert Vars
    with localconverter(robjects.default_converter + pandas2ri.converter):
        r_vector = robjects.conversion.py2rpy(residues['residues'].unique().tolist())
        background_resi = robjects.conversion.py2rpy(background_resi)

    # Get the background distribution
    globalenv['background'] = evotrace.GetSCWBackgound(
        pdb_file = pdb_path,
        chain = chain,
        dist_cutoff = dist_cutoff,
        resi = background_resi
    )
    # Compute the Z-score
    globalenv['z_score'] = evotrace.ComputeSCWzscore(
        globalenv['background'],
        resi = r_vector,
        output_df = True
    )
    # Convert globalenv['z_score'] to a pandas DataFrame
    with localconverter(robjects.default_converter + pandas2ri.converter):
        z_score = robjects.conversion.rpy2py(globalenv['z_score'])
        z_score['dist'] = dist_cutoff
    return z_score

def protein_structures(variants: pd.DataFrame, gene: str, max_af:float = 1.0, min_af:float = 0.0, ea_lower:int = 0, ea_upper:int = 100, scw_chain:str = 'A', scw_plddt_cutoff: int = 50, scw_min_dist_cutoff:int = 4, scw_max_dist_cutoff:int = 12, savepath: str=False) -> (pd.DataFrame, pd.DataFrame): # type: ignore
    """
    Generate AlphaFold protein structures for the given variants and save them to a specified path.

    Args:
        variants (pd.DataFrame): DataFrame containing variant information.
        gene (str): Gene name for which protein structures are generated.
        savepath (str): Path to save the generated protein structures.
        max_af (float, optional): Maximum allele frequency threshold for variant filtering. Defaults to 1.0.
        min_af (float, optional): Minimum allele frequency threshold for variant filtering. Defaults to 0.0.
        ea_lower (int, optional): Lower bound of the effect allele count threshold for variant filtering. Defaults to 0.
        ea_upper (int, optional): Upper bound of the effect allele count threshold for variant filtering. Defaults to 100.

    Returns:
        None
    """
    # Separate into case and control variants
    case_vars, cont_vars = _filter_variants(variants, gene, max_af, min_af, ea_lower, ea_upper)
    # Clean variant annotations
    case_vars_collapsed, cont_vars_collapsed = _clean_variant_formats(case_vars), _clean_variant_formats(cont_vars)
    prot_id = case_vars.loc[0, 'ENSP']
    prot_id = _check_pdb_id(prot_id)
    gene = case_vars.loc[0, 'gene']
    case_vars_residues, cont_vars_residues = _prepare_resi_df(case_vars_collapsed), _prepare_resi_df(cont_vars_collapsed)
    all_residues = pd.concat([case_vars_residues, cont_vars_residues])

    # Run SCW analysis
    # Get the protein file and save it to a temp directory
    background_resi, pdb_path = _get_plddt(prot_id, gene, scw_chain, scw_plddt_cutoff, savepath)
    # Run analysis
    results = []
    for residue in [all_residues, case_vars_residues, cont_vars_residues]:
        for dist_cutoff in range(scw_min_dist_cutoff, scw_max_dist_cutoff + 1):
            result = _scw_analysis(pdb_path, background_resi, all_residues, scw_chain, dist_cutoff)
            results.append(result)
    all_output_df = pd.concat(results[0])
    case_output_df = pd.concat(results[1])
    cont_output_df = pd.concat(results[2])
    # Plot results


    # Run if savepath is definedf
    if savepath:
        # Change the save paths
        savepath = _fix_savepath(savepath)
        new_savepath = os.path.join(savepath, f'ProteinStructures/{gene}/')
        os.makedirs(new_savepath, exist_ok=True)
        # Call the protein visualization
        new_savefile = os.path.join(new_savepath, f'{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_AlphaFold.pml')
        # Call the protein visualizations
        _r_alphafold_structure(case_vars_collapsed, cont_vars_collapsed, new_savefile)
        # Write case residues to output file
        with open(os.path.join(new_savepath, f'Cases_{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_residues.txt'), 'w') as f:
            for residue in case_vars_residues:
                residue = ''.join(filter(str.isdigit, residue))
                f.write(f'{residue}\n')
        # Write control residues to output file
        with open(os.path.join(new_savepath, f'Controls_{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_residues.txt'), 'a') as f:
            for residue in cont_vars_residues:
                residue = ''.join(filter(str.isdigit, residue))
                f.write(f'{residue}\n')
        # Save the SCW analysis output
        all_output_df.to_csv(os.path.join(new_savepath, f'All-Variants_{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_SCW_analysis.csv'), index=False)
        case_output_df.to_csv(os.path.join(new_savepath, f'Cases_{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_SCW_analysis.csv'), index=False)
        cont_output_df.to_csv(os.path.join(new_savepath, f'Controls_{gene}_AF{min_af}-{max_af}_EA{ea_lower}-{ea_upper}_SCW_analysis.csv'), index=False)

    if not savepath:
        print("No savepath provided. Skipping structure visualization.")

    return case_vars_residues, cont_vars_residues


#endregion
